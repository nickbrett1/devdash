"""The project join and the status strip.

Fixtures use synthetic project names (acme, example-one) on purpose: devdash is
a public repo, and a test that baked in a real private workspace name would
publish it. Only shapes and arithmetic matter here.
"""

import datetime as dt
import os
import subprocess
import threading
import time

import pytest

from devdash import projects

UTC = dt.UTC


@pytest.fixture(autouse=True)
def clean_caches():
    # The memos and the build cache outlive a request on purpose, so every case
    # starts from an empty one.
    projects.reset()
    yield
    projects.reset()


def _completed(stdout="", returncode=0, stderr=""):
    return subprocess.CompletedProcess(args=["docker"], returncode=returncode, stdout=stdout, stderr=stderr)


# -- pure helpers -----------------------------------------------------------


@pytest.mark.parametrize("text,expected", [
    ("0B", 0.0),
    ("512MiB", 512 * 2 ** 20),
    ("1.5GiB", 1.5 * 2 ** 30),
    ("1.234GB", 1.234e9),
    ("2TiB", 2 * 2 ** 40),
    ("", 0.0),
    ("garbage", 0.0),
])
def test_mem_bytes_handles_the_units_docker_mixes(text, expected):
    assert projects._mem_bytes(text) == expected


def test_parse_iso_is_utc_aware():
    when = projects._parse_iso("2026-09-01T12:00:00.000000000Z")
    assert when.tzinfo is UTC
    assert when.hour == 12


def test_parse_iso_is_forgiving():
    assert projects._parse_iso(None) is None
    assert projects._parse_iso("not a date") is None


def test_age_days():
    now = dt.datetime(2026, 9, 10, tzinfo=UTC)
    assert projects._age_days(None, now) is None
    assert projects._age_days(dt.datetime(2026, 9, 8, tzinfo=UTC), now) == pytest.approx(2.0)


def test_pipeline_map_overrides_the_name():
    assert projects.pipeline_for("acme", {}) == "acme"
    assert projects.pipeline_for("acme", {"pipeline_map": {"acme": "acme-monorepo"}}) == "acme-monorepo"


# -- the join ---------------------------------------------------------------


def _fake_reap_config(workspaces_dir, **overrides):
    cfg = {
        "workspaces_dir": str(workspaces_dir),
        "buildkite_org": "nick-brett",
        "buildkite_token": "tok",
        "pipeline_map": {},
    }
    cfg.update(overrides)
    return lambda home=None: cfg


def test_rows_joins_workspaces_containers_windows_and_builds(tmp_path, monkeypatch):
    acme = tmp_path / "acme"
    one = tmp_path / "example-one"
    acme.mkdir()
    one.mkdir()

    containers = [{
        "id": "c1", "name": "example-one-dev", "workspace": str(one),
        "running": True, "started_at": "2026-09-29T00:00:00Z",
    }]
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", lambda: containers)
    monkeypatch.setattr(projects.containers, "active_session", lambda cid: (True, "tmux: attached"))
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: ({str(acme)}, None))
    monkeypatch.setattr(projects.vscode, "list_window_titles",
                        lambda process=None: (["acme — README.md", "example-one-remote"], None))

    def fake_build(org, slug, token, **kw):
        assert org == "nick-brett" and token == "tok"
        if slug == "acme":
            return {"created_at": "2026-09-08T00:00:00Z", "number": 7,
                    "branch": "main", "author": "Nick Brett"}
        return None

    monkeypatch.setattr(projects.buildkite, "last_human_build", fake_build)

    now = dt.datetime(2026, 9, 10, tzinfo=UTC)
    rows, meta = projects.rows(now=now)

    assert [r["name"] for r in rows] == ["example-one", "acme"]  # running first

    a = rows[1]
    assert a["state"] == "absent"
    assert a["container"] is None
    assert a["live_session"] is False
    assert a["window_open"] is True
    assert a["pipeline"] == "acme"
    assert a["last_build"] == "#7 (Nick Brett, main)"
    assert a["last_human_build_days"] == pytest.approx(2.0)

    e = rows[0]
    assert e["state"] == "running"
    assert e["container"] == "example-one-dev"
    assert e["live_session"] is True
    assert e["live_evidence"] == "tmux: attached"
    assert e["window_open"] is False
    assert e["last_build"] is None
    assert e["last_human_build_days"] is None

    # The raw title count is separate from the matched set: it is the only
    # signal that distinguishes "no windows" from "cannot see windows".
    assert meta == {"window_error": None, "window_titles": 2}


def test_rows_are_ordered_running_then_stopped_then_absent(tmp_path, monkeypatch):
    """Running first, then stopped, then absent — and by name inside each
    group, so a row only moves when its container does."""
    for name in ("alpha", "bravo", "charlie", "mike", "yankee", "zulu"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", lambda: [
        {"id": "c1", "name": "zulu-dev", "workspace": str(tmp_path / "zulu"),
         "running": True, "started_at": None},
        {"id": "c2", "name": "mike-dev", "workspace": str(tmp_path / "mike"),
         "running": True, "started_at": None},
        {"id": "c3", "name": "alpha-dev", "workspace": str(tmp_path / "alpha"),
         "running": False, "started_at": None},
        {"id": "c4", "name": "bravo-dev", "workspace": str(tmp_path / "bravo"),
         "running": False, "started_at": None},
    ])
    monkeypatch.setattr(projects.containers, "active_session", lambda cid: (False, ""))
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    rows, _ = projects.rows(probe_builds=False)

    assert [(r["name"], r["state"]) for r in rows] == [
        ("mike", "running"), ("zulu", "running"),
        ("alpha", "stopped"), ("bravo", "stopped"),
        ("charlie", "absent"), ("yankee", "absent"),
    ]


def test_rows_carry_each_containers_memory(tmp_path, monkeypatch):
    """The figure comes from the same docker stats sample the strip shows, so a
    row and the footprint cannot disagree."""
    for name in ("acme", "example-one"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", lambda: [
        {"id": "c1", "name": "acme-dev", "workspace": str(tmp_path / "acme"),
         "running": True, "started_at": None},
        {"id": "c2", "name": "example-one-dev", "workspace": str(tmp_path / "example-one"),
         "running": False, "started_at": None},
    ])
    monkeypatch.setattr(projects.containers, "active_session", lambda cid: (False, ""))
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    rows, _ = projects.rows(probe_builds=False, memory={"c1": 412 * 2 ** 20})

    # c2 is stopped, so docker stats never mentions it — and it is using
    # nothing, which is not the same as unknown.
    assert {r["name"]: r["mem_bytes"] for r in rows} == {
        "acme": 412 * 2 ** 20, "example-one": None,
    }


def test_rows_have_no_memory_when_no_sample_was_paid_for(tmp_path, monkeypatch):
    """A caller that did not ask for a sample gets no column, rather than a
    hidden two-second docker read inside what looks like a pure join."""
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", lambda: [
        {"id": "c1", "name": "acme-dev", "workspace": str(tmp_path / "acme"),
         "running": True, "started_at": None},
    ])
    monkeypatch.setattr(projects.containers, "active_session", lambda cid: (False, ""))
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))
    monkeypatch.setattr(projects, "_docker", lambda args, timeout=30: pytest.fail("no sample was asked for"))

    rows, _ = projects.rows(probe_builds=False)

    assert [r["mem_bytes"] for r in rows] == [None]


def test_memory_is_the_sample_the_status_strip_uses(monkeypatch):
    """One stats call answers both, which is why the per-row figures add up to
    the footprint."""
    monkeypatch.setattr(projects, "_docker", _fake_docker)

    assert projects.memory() == {
        "aaa": 1 * 2 ** 30, "bbb": 512 * 2 ** 20, "ccc": 256 * 2 ** 20,
    }


def test_rows_keeps_a_container_whose_workspace_is_gone(tmp_path, monkeypatch):
    """A moved/deleted workspace still holds RAM, so it must not vanish."""
    ghost = tmp_path / "ghost"
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", lambda: [{
        "id": "c9", "name": "ghost-dev", "workspace": str(ghost),
        "running": False, "started_at": None,
    }])
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    rows, _ = projects.rows(probe_builds=False)

    assert [r["name"] for r in rows] == ["ghost"]
    assert rows[0]["state"] == "stopped"
    assert rows[0]["path"] == str(ghost)


def test_rows_degrades_when_system_events_is_unavailable(tmp_path, monkeypatch):
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", list)
    monkeypatch.setattr(projects.vscode, "windows_for",
                        lambda ws, process=None: (set(), "not authorized"))
    monkeypatch.setattr(projects.vscode, "list_window_titles",
                        lambda process=None: ([], "not authorized"))

    rows, meta = projects.rows(probe_builds=False)

    assert rows[0]["window_open"] is False
    assert meta["window_error"] == "not authorized"
    assert meta["window_titles"] == 0


def test_probe_builds_false_makes_no_network_call(tmp_path, monkeypatch):
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", list)
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))
    monkeypatch.setattr(projects.buildkite, "last_human_build",
                        lambda *a, **k: pytest.fail("should not be called"))

    rows, _ = projects.rows(probe_builds=False)
    assert rows[0]["last_build"] is None


def test_last_build_survives_a_buildkite_error(tmp_path, monkeypatch):
    """A missing pipeline is not a broken project list."""
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", list)
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    def boom(*a, **k):
        raise projects.buildkite.BuildkiteError("pipeline 'acme' not found")

    monkeypatch.setattr(projects.buildkite, "last_human_build", boom)

    rows, _ = projects.rows()
    assert rows[0]["last_build"] is None


def test_build_lookups_happen_in_parallel(tmp_path, monkeypatch):
    """One HTTPS call per project, so a cold first poll used to wait for ten of
    them one after another — most of the several seconds it took."""
    for name in ("acme", "example-one", "third"):
        (tmp_path / name).mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", list)
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    def slow_build(org, slug, token, **kw):
        time.sleep(0.2)
        return {"created_at": "2026-09-08T00:00:00Z", "number": 1, "branch": "main", "author": "a"}

    monkeypatch.setattr(projects.buildkite, "last_human_build", slow_build)

    started = time.monotonic()
    rows, _ = projects.rows()
    elapsed = time.monotonic() - started

    assert len(rows) == 3
    assert elapsed < 0.5, f"three 0.2s lookups took {elapsed:.2f}s — they are not in parallel"


def test_last_build_is_cached(tmp_path, monkeypatch):
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    monkeypatch.setattr(projects.containers, "list_devcontainers", list)
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    calls = []

    def counting(org, slug, token, **kw):
        calls.append(slug)
        return {"created_at": "2026-09-08T00:00:00Z", "number": 1, "branch": "main", "author": "a"}

    monkeypatch.setattr(projects.buildkite, "last_human_build", counting)

    projects.rows()
    projects.rows()
    assert calls == ["acme"]  # the second call came from the cache


# -- the status strip -------------------------------------------------------


def _fake_docker(args, timeout=30):
    """A docker that reports a 15.66 GiB VM with three containers running."""
    if args[:1] == ["info"]:
        return _completed("16819609600")
    if args[:1] == ["stats"]:
        return _completed("aaa 1GiB / 2GiB\nbbb 512MiB / 2GiB\nccc 256MiB / 2GiB\n")
    return _completed("", returncode=1)


def test_status_sums_only_devcontainer_footprint(monkeypatch):
    monkeypatch.setattr(projects, "_docker", _fake_docker)

    status = projects.status(devcontainer_ids=["aaa", "bbb"])

    assert status["vm_mem_total"] == 16819609600
    assert status["vm_mem_used"] == pytest.approx(1 * 2 ** 30 + 512 * 2 ** 20 + 256 * 2 ** 20)
    # ccc is running but is not a devcontainer, so it is not in either count.
    assert status["devcontainer_footprint"] == pytest.approx(1 * 2 ** 30 + 512 * 2 ** 20)
    assert status["container_count"] == 2
    assert status["running_count"] == 3


def test_status_is_not_fatal_when_docker_is_missing(monkeypatch):
    def no_docker(args, timeout=30):
        raise FileNotFoundError("docker")

    monkeypatch.setattr(projects, "_docker", no_docker)
    status = projects.status(devcontainer_ids=[])

    assert status == {
        "vm_mem_total": None, "vm_mem_used": 0.0,
        "container_count": 0, "running_count": 0, "devcontainer_footprint": 0.0,
    }


def test_workspace_dirs_ignores_dotfiles_and_files(tmp_path):
    (tmp_path / "acme").mkdir()
    (tmp_path / ".hidden").mkdir()
    (tmp_path / "notes.txt").write_text("x")
    assert projects._workspace_dirs(tmp_path) == ["acme"]
    assert projects._workspace_dirs(os.path.join(tmp_path, "nope")) == []


# -- the memo ---------------------------------------------------------------
#
# These reads are the reason a poll is slow: `docker stats` is ~2s and the
# container list is >1s, and neither moves much in thirty seconds. The memo is
# what turns a poll into a join over the last sample, so its behaviour is worth
# pinning down.


def _counting(fn):
    calls = []

    def wrapped(*a, **kw):
        calls.append(a)
        return fn(*a, **kw)

    wrapped.calls = calls
    return wrapped


def test_the_container_list_is_read_once_for_two_polls(tmp_path, monkeypatch):
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    listing = _counting(list)
    monkeypatch.setattr(projects.containers, "list_devcontainers", listing)
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    projects.rows(probe_builds=False)
    projects.rows(probe_builds=False)

    assert len(listing.calls) == 1


def test_invalidate_makes_the_next_read_fresh(tmp_path, monkeypatch):
    """The phone reloads the instant a container stops, and must not be shown
    the row it just changed."""
    (tmp_path / "acme").mkdir()
    monkeypatch.setattr(projects.reap_config, "load", _fake_reap_config(tmp_path))
    listing = _counting(list)
    monkeypatch.setattr(projects.containers, "list_devcontainers", listing)
    monkeypatch.setattr(projects.vscode, "windows_for", lambda ws, process=None: (set(), None))
    monkeypatch.setattr(projects.vscode, "list_window_titles", lambda process=None: ([], None))

    projects.rows(probe_builds=False)
    projects.invalidate()
    projects.rows(probe_builds=False)

    assert len(listing.calls) == 2


def test_a_cold_failure_is_raised_rather_than_swallowed():
    """No value yet and a broken read is a broken page, not a blank one."""
    def boom():
        raise OSError("docker is not reachable")

    memo = projects._Memo(3.0, boom)
    with pytest.raises(OSError, match="not reachable"):
        memo.get()


def test_a_failed_refresh_keeps_the_last_good_value():
    """A blip on the second read must not blank a page that has data."""
    calls = []

    def flaky():
        calls.append(1)
        if len(calls) == 1:
            return "first"
        raise OSError("docker went away")

    memo = projects._Memo(0.0, flaky)  # 0 TTL: every read refreshes
    assert memo.get() == "first"

    for _ in range(500):  # let the failing refresh land
        memo.get()  # past its TTL, so this asks for a refresh
        if len(calls) >= 2 and not memo._refreshing:
            break
        time.sleep(0.01)

    assert len(calls) >= 2, "the refresh should have been attempted"
    assert memo.get() == "first"


def test_a_stale_value_is_served_while_it_refreshes():
    """The point of the memo: an answer does not wait for the read."""
    gate = threading.Event()

    def slow():
        gate.wait(5)
        return "fresh"

    memo = projects._Memo(0.0, slow)  # 0 TTL, so the value is stale at once
    memo._value, memo._at = "stale", time.monotonic()

    started = time.monotonic()
    assert memo.get() == "stale"
    assert time.monotonic() - started < 0.5, "the caller should not wait for the refresh"
    gate.set()


def test_live_sessions_are_probed_for_every_running_container(tmp_path, monkeypatch):
    """In parallel, and once each: the probe is three `docker exec`s, so a
    project list of ten used to pay for thirty."""
    ids = []

    def probing(cid):
        ids.append(cid)
        return (True, f"{cid}: tmux")

    monkeypatch.setattr(projects.containers, "active_session", probing)

    lives = projects._live_sessions([{"id": f"c{i}", "running": True} for i in range(5)])

    assert sorted(ids) == [f"c{i}" for i in range(5)]
    assert lives["c3"] == (True, "c3: tmux")


def test_live_sessions_costs_nothing_with_no_containers(monkeypatch):
    monkeypatch.setattr(projects.containers, "active_session",
                        lambda cid: pytest.fail("nothing is running"))
    assert projects._live_sessions([]) == {}
