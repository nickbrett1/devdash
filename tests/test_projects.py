"""The project join and the status strip.

Fixtures use synthetic project names (acme, example-one) on purpose: devdash is
a public repo, and a test that baked in a real private workspace name would
publish it. Only shapes and arithmetic matter here.
"""

import datetime as dt
import os
import subprocess

import pytest

from devdash import projects

UTC = dt.UTC


@pytest.fixture(autouse=True)
def empty_build_cache():
    projects._build_cache.clear()
    yield
    projects._build_cache.clear()


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

    assert [r["name"] for r in rows] == ["acme", "example-one"]  # sorted

    a = rows[0]
    assert a["state"] == "absent"
    assert a["container"] is None
    # No container, so nothing to reach: a Blink link would be a lie.
    assert a["blink"] is None
    assert a["live_session"] is False
    assert a["window_open"] is True
    assert a["pipeline"] == "acme"
    assert a["last_build"] == "#7 (Nick Brett, main)"
    assert a["last_human_build_days"] == pytest.approx(2.0)

    e = rows[1]
    assert e["state"] == "running"
    assert e["container"] == "example-one-dev"
    # devopen registers the container under the workspace name, so the phone
    # link and the project agree by construction; remoteUser defaults to vscode.
    assert e["blink"] == "blink://host/?host=example-one&username=vscode&port=22"
    assert e["live_session"] is True
    assert e["live_evidence"] == "tmux: attached"
    assert e["window_open"] is False
    assert e["last_build"] is None
    assert e["last_human_build_days"] is None

    # The raw title count is separate from the matched set: it is the only
    # signal that distinguishes "no windows" from "cannot see windows".
    assert meta == {"window_error": None, "window_titles": 2}


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


def test_status_sums_only_devcontainer_footprint(monkeypatch):
    def fake_docker(args, timeout=30):
        if args[:1] == ["info"]:
            return _completed("16819609600")
        if args[:1] == ["stats"]:
            return _completed("aaa 1GiB / 2GiB\nbbb 512MiB / 2GiB\nccc 256MiB / 2GiB\n")
        return _completed("", returncode=1)

    monkeypatch.setattr(projects, "_docker", fake_docker)

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
