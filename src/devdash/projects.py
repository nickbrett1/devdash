"""Build the project rows — workspaces × containers × last human build.

This is the join the spec calls the project list. Every primitive comes from
devreap, so the definitions (what counts as a devcontainer, what counts as a
live session, what counts as a human build) have exactly one home.
"""

import datetime as dt
import os
import subprocess

from devreap import buildkite, containers
from devreap import config as reap_config
from devreap import vscode

# The last-human-build lookup is one HTTP call per project, and the UI
# refreshes. Six projects on a 5-minute TTL is cheaper than the same six on
# every poll, and a build older than the TTL is not news.
_BUILD_TTL = 300
_build_cache = {}


def _parse_iso(value):
    """Buildkite timestamps are UTC ISO-8601 with a Z."""
    if not value:
        return None
    try:
        return dt.datetime.strptime(value[:19], "%Y-%m-%dT%H:%M:%S").replace(tzinfo=dt.timezone.utc)
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
    if hit and (dt.datetime.now(dt.timezone.utc) - hit[0]).total_seconds() < _BUILD_TTL:
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
    _build_cache[key] = (dt.datetime.now(dt.timezone.utc), days, label)
    return days, label


def rows(now=None, probe_builds=True):
    """One dict per project, sorted by name. Read-only: no container is
    started, stopped or otherwise touched."""
    cfg = reap_config.load()
    now = now or dt.datetime.now(dt.timezone.utc)
    token = cfg.get("buildkite_token") or os.environ.get("BUILDKITE_API_TOKEN") or ""
    org = cfg.get("buildkite_org") or "nick-brett"
    workspaces_dir = os.path.expanduser(cfg.get("workspaces_dir") or "")

    devcontainers = containers.list_devcontainers()
    by_name = {os.path.basename(c["workspace"].rstrip("/")): c for c in devcontainers}

    # A project is anything we have a workspace directory for, plus anything
    # that has a container — a container whose workspace has been moved or
    # deleted is still holding RAM, so it must not vanish from the list.
    paths = {n: os.path.join(workspaces_dir, n) for n in _workspace_dirs(workspaces_dir)}
    for name, c in by_name.items():
        paths.setdefault(name, c["workspace"])

    # One System Events call for the whole list. A failure here (no
    # Accessibility grant, no GUI session) must degrade, not 500.
    open_windows, window_error = vscode.windows_for(list(paths.values()))

    out = []
    for name in sorted(paths):
        path = paths[name]
        c = by_name.get(name)
        state, live, evidence = "absent", False, ""
        if c:
            state = "running" if c["running"] else "stopped"
            if c["running"]:
                live, evidence = containers.active_session(c["id"])
        days, label = _last_build(org, pipeline_for(name, cfg), token, now) if probe_builds else (None, None)
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
    return out, {"window_error": window_error}


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
    return subprocess.run([containers.DOCKER] + args, capture_output=True, text=True, timeout=timeout)


def status(devcontainer_ids=None):
    """VM memory, container counts and the devcontainer footprint — the
    numbers that answer "do I need to close something"."""
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

    if devcontainer_ids is None:
        devcontainer_ids = [c["id"] for c in containers.list_devcontainers()]
    footprint = sum(bytes_ for cid, bytes_ in stats.items() if cid in set(devcontainer_ids))

    return {
        "vm_mem_total": mem_total,
        "vm_mem_used": used,
        "container_count": len(devcontainer_ids),
        "running_count": len(stats),
        "devcontainer_footprint": footprint,
    }
