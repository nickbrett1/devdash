"""Build the project rows — workspaces × containers × last human build.

This is the join the spec calls the project list. Every primitive comes from
devreap, so the definitions (what counts as a devcontainer, what counts as a
live session, what counts as a human build) have exactly one home.
"""

import datetime as dt
import os
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress

from devreap import buildkite, containers, vscode
from devreap import config as reap_config

# Reading order for the list: what is up, then what is down but present, then
# what is only a directory. A row's state is the thing the page exists to show,
# so it decides the order.
_STATE_RANK = {"running": 0, "stopped": 1, "absent": 2}

# The last-human-build lookup is one HTTP call per project, and the UI
# refreshes. Six projects on a 5-minute TTL is cheaper than the same six on
# every poll, and a build older than the TTL is not news.
_BUILD_TTL = 300
_build_cache = {}


class _Memo:
    """A slow read that answers from a cached value while it refreshes.

    Everything expensive here is a *sample* of a machine that barely changes
    between two polls: `docker stats` alone takes ~2s, and `docker ps` plus an
    inspect per container is another 1.2s. Making the phone wait for a number
    that has not moved since last time is the wrong trade, so after the first
    answer this returns the last one immediately and refreshes behind it —
    stale by at most one TTL, and never blocking.

    Only the first call blocks (there is nothing to serve yet), which is why
    `serve()` warms these at startup. A failed refresh keeps the last good
    value rather than blanking the page; a failure with *no* value yet is
    re-raised, so a broken join still looks like one.
    """

    def __init__(self, ttl, fn):
        self.ttl = ttl
        self.fn = fn  # a callable, not a bound method: tests monkeypatch these
        self._value = None
        self._at = 0.0
        self._error = None
        self._refreshing = False
        self._cond = threading.Condition()

    def get(self):
        with self._cond:
            if self._value is not None:
                if time.monotonic() - self._at > self.ttl and not self._refreshing:
                    self._start()
                return self._value
            # Nothing to serve yet: wait for the first sample rather than hand
            # a caller a half-built answer.
            while self._refreshing:
                self._cond.wait()
            if self._value is None and self._error is not None:
                raise self._error
            self._start()
            while self._refreshing:
                self._cond.wait()
            if self._value is None and self._error is not None:
                raise self._error
            return self._value

    def _start(self):
        """Call with the condition held."""
        self._refreshing = True
        threading.Thread(target=self._refresh, daemon=True, name="memo").start()

    def _refresh(self):
        try:
            value = self.fn()
        except Exception as e:  # noqa: BLE001 — reported below, never in a thread
            value, self._error = None, e
        else:
            self._error = None
        with self._cond:
            if value is not None:
                self._value = value
            # Move the clock even on failure, so a broken read retries on the
            # TTL rather than on every request.
            self._at = time.monotonic()
            self._refreshing = False
            self._cond.notify_all()

    def clear(self):
        """Forget the value, so the next caller waits for a fresh one. Used
        after an action: the phone reloads the moment a container stops, and it
        must not be shown the state it just changed."""
        with self._cond:
            self._value = None
            self._at = 0.0
            self._error = None


# `docker ps -a` plus four inspects per container is ~1.2s on this machine, and
# both /api/projects and /api/status need the same list, so they share one.
_containers = _Memo(3.0, lambda: containers.list_devcontainers())
# A docker stats sample: ~1.9s, and the numbers move slowly. One poll sees it;
# the next two are free.
_status_sample = _Memo(15.0, lambda: _sample_status())
_MEMOS = (_containers, _status_sample)


def warm():
    """Fill the caches off the request path. `serve()` calls this at startup so
    the first phone poll is fast too: a cold cache is a docker sample plus a
    Buildkite call per project, and paying that while someone watches a spinner
    is the wrong moment to pay it.

    The whole join runs, not just the memos, because the per-project build
    lookup is cached separately and is the other half of a cold poll. A docker
    that is not up yet is a slow page, not a dead server — the next request
    tries again and reports.
    """
    for memo in _MEMOS:
        with suppress(Exception):
            memo.get()
    with suppress(Exception):
        rows()


def invalidate():
    """Drop the cached samples after something changed on disk or in docker."""
    for memo in _MEMOS:
        memo.clear()


def reset():
    """Test hook: drop every memo between cases."""
    invalidate()
    _build_cache.clear()


def _parse_iso(value):
    """Buildkite timestamps are UTC ISO-8601 with a Z."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.UTC)
    except ValueError:
        return None


def _age_days(when, now):
    return None if when is None else (now - when).total_seconds() / 86400.0


def _workspace_dirs(workspaces_dir):
    try:
        return sorted(
            e for e in os.listdir(workspaces_dir)
            if not e.startswith(".") and os.path.isdir(os.path.join(workspaces_dir, e))
        )
    except OSError:
        return []


def pipeline_for(name, cfg):
    """The Buildkite pipeline a workspace belongs to (override map, else name)."""
    return (cfg.get("pipeline_map") or {}).get(name, name)


def _last_build(org, slug, token, now):
    """(days, label) for the last human build, cached, never fatal."""
    key = (org, slug)
    hit = _build_cache.get(key)
    if hit and (dt.datetime.now(dt.UTC) - hit[0]).total_seconds() < _BUILD_TTL:
        return hit[1], hit[2]
    days, label = None, None
    if token:
        try:
            last = buildkite.last_human_build(org, slug, token)
        except buildkite.BuildkiteError:
            last = None
        if last:
            days = _age_days(_parse_iso(last["created_at"]), now)
            label = f"#{last['number']} ({last['author']}, {last['branch']})"
    _build_cache[key] = (dt.datetime.now(dt.UTC), days, label)
    return days, label


def rows(now=None, probe_builds=True):
    """One dict per project: running first, then stopped, then absent, each
    group by name. Read-only: no container is started, stopped or otherwise
    touched.

    The order is the answer to the only question the page is opened to ask —
    what is up, and what is not — so it is decided here rather than left to
    each reader to sort for itself.
    """
    cfg = reap_config.load()
    now = now or dt.datetime.now(dt.UTC)
    token = cfg.get("buildkite_token") or os.environ.get("BUILDKITE_API_TOKEN") or ""
    org = cfg.get("buildkite_org") or "nick-brett"
    workspaces_dir = os.path.expanduser(cfg.get("workspaces_dir") or "")

    devcontainers = _containers.get()
    by_name = {os.path.basename(c["workspace"].rstrip("/")): c for c in devcontainers}

    # A project is anything we have a workspace directory for, plus anything
    # that has a container — a container whose workspace has been moved or
    # deleted is still holding RAM, so it must not vanish from the list.
    paths = {n: os.path.join(workspaces_dir, n) for n in _workspace_dirs(workspaces_dir)}
    for name, c in by_name.items():
        paths.setdefault(name, c["workspace"])

    running = [c for c in devcontainers if c["running"]]

    # Three independent waits, run at once rather than one after the other:
    # System Events (two osascript calls, ~0.4s), the live-session probe (three
    # `docker exec`s per running container, ~0.25s each — serial that was the
    # single biggest cost of a poll, and it grows with the number of projects),
    # and the per-project Buildkite lookup (one HTTPS call each, ~0.3s, which
    # is what a *cold* first poll spends its seconds on). The two System Events
    # calls stay in one thread: they share System Events, and serialising them
    # keeps the grant check honest.
    with ThreadPoolExecutor(max_workers=3) as pool:
        windows = pool.submit(_windows, paths)
        sessions = pool.submit(_live_sessions, running)
        builds = pool.submit(_last_builds, sorted(paths), org, cfg, token, now) if probe_builds else None
        open_windows, window_error, titles = windows.result()
        lives = sessions.result()
        last_builds = builds.result() if builds else {}

    out = []
    for name in sorted(paths):
        path = paths[name]
        c = by_name.get(name)
        state = "absent" if not c else ("running" if c["running"] else "stopped")
        live, evidence = lives.get(c["id"], (False, "")) if c else (False, "")
        days, label = last_builds.get(name, (None, None))
        out.append({
            "name": name,
            "path": path,
            "container": c["name"] if c else None,
            "state": state,
            "live_session": live,
            "live_evidence": evidence,
            "window_open": path in open_windows,
            "pipeline": pipeline_for(name, cfg),
            "last_human_build_days": days,
            "last_build": label,
        })
    # Running, then stopped, then absent — the order the phone wants them in —
    # and by name inside each group, so a row only moves when its container
    # does.
    out.sort(key=lambda r: (_STATE_RANK[r["state"]], r["name"]))
    return out, {"window_error": window_error, "window_titles": len(titles)}


def _last_builds(names, org, cfg, token, now):
    """{name: (days, label)} — one Buildkite call per project, in parallel.

    A cold cache means one HTTPS call per project: ten of them is the several
    seconds the *first* poll after a restart used to spend, waiting on ten
    round trips one after another. They are independent, so they go at once.
    """
    if not names:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(names))) as pool:
        futures = {n: pool.submit(_last_build, org, pipeline_for(n, cfg), token, now)
                   for n in names}
    return {n: f.result() for n, f in futures.items()}


def _windows(paths):
    """(open set, error, titles). One System Events call for the whole list — a
    failure here (no Accessibility grant, no GUI session) must degrade, not
    500 — plus the deliberately separate raw title count that tells "no windows"
    apart from "cannot see windows"."""
    open_windows, window_error = vscode.windows_for(list(paths.values()))
    titles, _ = vscode.list_window_titles()
    return open_windows, window_error, titles


def _live_sessions(running):
    """{container id: (live, evidence)} for every running container, in
    parallel: `active_session` is three `docker exec`s, and they are
    independent, so the wait is one exec rather than three per project."""
    if not running:
        return {}
    with ThreadPoolExecutor(max_workers=min(8, len(running))) as pool:
        futures = {c["id"]: pool.submit(containers.active_session, c["id"]) for c in running}
    return {cid: f.result() for cid, f in futures.items()}


# --------------------------------------------------------------------------
# Status strip
# --------------------------------------------------------------------------

_UNITS = {
    "B": 1, "kB": 1000, "KiB": 1024, "MB": 10 ** 6, "MiB": 2 ** 20,
    "GB": 10 ** 9, "GiB": 2 ** 30, "TB": 10 ** 12, "TiB": 2 ** 40,
}


def _mem_bytes(text):
    """'1.234GiB' / '512MiB' / '0B' → bytes. Docker mixes SI and IEC units."""
    text = (text or "").strip()
    for unit in ("TiB", "GiB", "MiB", "KiB", "TB", "GB", "MB", "kB", "B"):
        if text.endswith(unit):
            try:
                return float(text[: -len(unit)]) * _UNITS[unit]
            except ValueError:
                return 0.0
    return 0.0


def _docker(args, timeout=30):
    return subprocess.run([containers.DOCKER] + args, capture_output=True, text=True,
                          timeout=timeout, check=False)


def status(devcontainer_ids=None):
    """VM memory, container counts and the devcontainer footprint — the
    numbers that answer "do I need to close something".

    The docker calls come from a memo, so this is a join over two cached
    samples and a count; only a cold cache waits."""
    sample = _status_sample.get()
    stats = sample["stats"]
    if devcontainer_ids is None:
        devcontainer_ids = [c["id"] for c in _containers.get()]
    footprint = sum(bytes_ for cid, bytes_ in stats.items() if cid in set(devcontainer_ids))

    return {
        "vm_mem_total": sample["mem_total"],
        "vm_mem_used": sample["used"],
        "container_count": len(devcontainer_ids),
        "running_count": len(stats),
        "devcontainer_footprint": footprint,
    }


def _sample_status():
    """The two docker calls that take time: VM memory, and a stats snapshot.

    `docker stats --no-stream` is ~1.9s on its own — it is the floor on this
    endpoint, which is exactly why it is sampled rather than awaited per poll.
    Every failure degrades to a zero rather than a 500: a missing docker must
    not take the page down with it.
    """
    mem_total = None
    try:
        r = _docker(["info", "--format", "{{.MemTotal}}"], timeout=20)
        if r.returncode == 0 and r.stdout.strip().isdigit():
            mem_total = int(r.stdout.strip())
    except (OSError, subprocess.SubprocessError):
        pass

    used = 0.0
    stats = {}
    try:
        r = _docker(["stats", "--no-stream", "--format", "{{.ID}} {{.MemUsage}}"])
        if r.returncode == 0:
            for line in r.stdout.splitlines():
                cid, _, usage = line.strip().partition(" ")
                if not cid:
                    continue
                used += _mem_bytes(usage.split("/")[0])
                stats[cid] = _mem_bytes(usage.split("/")[0])
    except (OSError, subprocess.SubprocessError):
        pass

    return {"mem_total": mem_total, "used": used, "stats": stats}
