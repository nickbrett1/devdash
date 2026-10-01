"""Open and close — the only mutating code in devdash.

Synthetic names (acme, example-one) throughout: this repo is public, and a
fixture that named a real workspace would publish it. What is being tested is
the *policy* — what devdash refuses, what it passes on, and what it does to the
two halves of Close when only one of them can succeed.
"""

import pytest

from devdash import actions, projects


def _row(**over):
    row = {
        "name": "acme",
        "path": "/w/acme",
        "container": "acme-dev",
        "state": "running",
        "live_session": False,
        "live_evidence": "",
        "window_open": False,
        "pipeline": "acme",
        "last_human_build_days": None,
        "last_build": None,
    }
    row.update(over)
    return row


@pytest.fixture
def rows(monkeypatch):
    """Patch the join so an action sees exactly one project. Returns a setter
    so a test can reshape the row after setup."""
    state = {"row": _row()}

    def fake_rows(probe_builds=True):
        assert probe_builds is False, "an action must never pay for a Buildkite call"
        return [state["row"]], {"window_error": None, "window_titles": 0}

    monkeypatch.setattr(projects, "rows", fake_rows)
    monkeypatch.setattr(actions.reap_config, "load", lambda: {"workspaces_dir": "/w"})
    # Default: no tailscale authkey configured, so `open` stays silent.
    monkeypatch.setattr(actions.devopen_config, "load", lambda: {"tailscale_authkey": ""})
    return state


@pytest.fixture
def calls(monkeypatch):
    """Record the container/vscode primitives an action reaches for."""
    log = {"stop": [], "close": []}
    monkeypatch.setattr(actions.containers, "stop",
                        lambda cid, timeout=60: (log["stop"].append(cid) or (True, "acme-dev")))
    monkeypatch.setattr(actions.vscode, "close_workspace_window",
                        lambda ws, process=None: (log["close"].append(ws) or (True, "closed")))
    return log


# -- close ------------------------------------------------------------------


def test_close_refuses_a_live_session_by_default(rows, calls):
    """The veto is the default, and the answer says why — a phone user who is
    told 'live session (tmux: attached)' knows to tap Force, one who is told
    'error' does not."""
    rows["row"] = _row(live_session=True, live_evidence="tmux: attached")
    out = actions.close_project("acme")
    assert out["refused"] is True
    assert out["stopped"] is False
    assert "tmux: attached" in out["detail"]
    assert "force" in out["detail"]
    assert calls["stop"] == [] and calls["close"] == []


def test_close_with_force_stops_a_live_session(rows, calls):
    rows["row"] = _row(live_session=True, live_evidence="tmux: attached")
    out = actions.close_project("acme", force=True)
    assert out.get("refused") is None
    assert out["stopped"] is True
    assert calls["stop"] == ["acme-dev"]


def test_close_stops_a_running_container_and_closes_its_window(rows, calls):
    rows["row"] = _row(state="running", window_open=True)
    out = actions.close_project("acme")
    assert out == {"name": "acme", "stopped": True, "window_closed": True,
                   "container": "acme-dev", "detail": "closed"}
    assert calls["stop"] == ["acme-dev"]
    assert calls["close"] == ["/w/acme"]


def test_close_still_closes_the_window_of_a_stopped_container(rows, calls):
    """The two halves are independent: a container that is already stopped is
    not a failure, and its window is exactly what is still holding RAM."""
    rows["row"] = _row(state="stopped", window_open=True)
    out = actions.close_project("acme")
    assert out["stopped"] is True
    assert out["window_closed"] is True
    assert calls["stop"] == []
    assert calls["close"] == ["/w/acme"]


def test_close_attempts_the_window_even_when_it_looks_closed(rows, calls):
    """window_open is 'System Events can see it now', and System Events answers
    [] while the screen is locked. A false there must not skip the close."""
    rows["row"] = _row(state="running", window_open=False)
    actions.close_project("acme")
    assert calls["close"] == ["/w/acme"]


def test_close_reports_the_window_half_when_it_cannot_close(rows, calls, monkeypatch):
    """A 'save your changes?' sheet keeps the window open. The container is
    stopped either way, so this is a partial success with an honest detail —
    not a failed request that leaves the caller unsure whether RAM came back."""
    monkeypatch.setattr(actions.vscode, "close_workspace_window",
                        lambda ws, process=None: (False, "window still open after close (unsaved changes?)"))
    out = actions.close_project("acme")
    assert out["stopped"] is True
    assert out["window_closed"] is False
    assert "unsaved changes" in out["detail"]


def test_close_of_an_absent_project_touches_nothing(rows, calls):
    rows["row"] = _row(state="absent", container=None)
    out = actions.close_project("acme")
    assert out["stopped"] is False
    assert out["window_closed"] is False
    assert "no container" in out["detail"]
    assert calls["stop"] == [] and calls["close"] == []


# -- open -------------------------------------------------------------------


def test_open_reuses_devopen_with_the_safe_defaults(rows, monkeypatch):
    seen = {}

    def fake_open(repo_url, **kw):
        seen["repo_url"] = repo_url
        seen.update(kw)
        return "vscode-remote://dev-container+abc/acme"

    monkeypatch.setattr(actions.opener, "open_repo", fake_open)
    out = actions.open_project("acme")
    assert out["uri"].startswith("vscode-remote://")
    assert seen["repo_url"] == "acme"
    assert seen["workspaces_dir"] == "/w"
    # A server cannot answer a prompt, so tailscale must never be None ("ask").
    assert seen["tailscale"] is False
    assert seen["fresh"] is False and seen["clean"] is False


def test_open_registers_tailscale_when_there_is_a_key(rows, monkeypatch):
    """Registering is most of the point of opening from a phone — ssh, a
    terminal app or VS Code over the tailnet all need a MagicDNS name. Without
    a key, `tailscale up` wants a browser and the server cannot help, so it
    stays silent rather than hanging."""
    seen = {}
    monkeypatch.setattr(actions.opener, "open_repo",
                        lambda repo_url, **kw: seen.update(kw) or "uri")
    monkeypatch.setattr(actions.devopen_config, "load", lambda: {"tailscale_authkey": "tskey-x"})
    actions.open_project("acme")
    assert seen["tailscale"] is True

    monkeypatch.setattr(actions.devopen_config, "load", lambda: {"tailscale_authkey": "  "})
    actions.open_project("acme")
    assert seen["tailscale"] is False


def test_open_threads_fresh_and_clean_through(rows, monkeypatch):
    seen = {}
    monkeypatch.setattr(actions.opener, "open_repo",
                        lambda repo_url, **kw: seen.update(kw) or "uri")
    actions.open_project("acme", fresh=True, clean=True)
    assert seen["fresh"] is True and seen["clean"] is True


def test_open_collects_the_log_instead_of_printing_it(rows, monkeypatch):
    """Under launchd there is no terminal; the tail of devopen's output is the
    only thing that can explain a slow or failed open to the phone."""
    def fake_open(repo_url, on_log, **kw):
        on_log("Cloning acme → /w/acme")
        on_log("Container ready: abc123")
        return "uri"

    monkeypatch.setattr(actions.opener, "open_repo", fake_open)
    out = actions.open_project("acme")
    assert out["log"] == ["Cloning acme → /w/acme", "Container ready: abc123"]


def test_open_turns_a_devopen_failure_into_an_action_error(rows, monkeypatch):
    def boom(repo_url, **kw):
        raise actions.opener.DevopenError("command failed (128): git clone")

    monkeypatch.setattr(actions.opener, "open_repo", boom)
    with pytest.raises(actions.ActionError) as excinfo:
        actions.open_project("acme")
    assert "git clone" in str(excinfo.value)


# -- provision ---------------------------------------------------------------


def test_provision_expands_a_repo_and_reports_the_project_name(rows, monkeypatch):
    """`owner/name` becomes a URL, and the workspace directory devopen will
    create is the repo's basename — that is the name the project list will
    show once this finishes, so it is the name worth returning."""
    seen = {}
    monkeypatch.setattr(actions.opener, "open_repo",
                        lambda repo_url, **kw: seen.update(repo_url=repo_url, **kw) or "uri")
    out = actions.provision("nickbrett1/acme", on_log=lambda line: None)
    assert seen["repo_url"] == "https://github.com/nickbrett1/acme.git"
    assert seen["workspaces_dir"] == "/w"
    assert out == {"name": "acme", "repo": "https://github.com/nickbrett1/acme.git", "uri": "uri"}


def test_provision_passes_a_url_through_unchanged(rows, monkeypatch):
    seen = {}
    monkeypatch.setattr(actions.opener, "open_repo",
                        lambda repo_url, **kw: seen.update(repo_url=repo_url) or "uri")
    actions.provision("git@github.com:nickbrett1/acme.git", on_log=lambda line: None)
    assert seen["repo_url"] == "git@github.com:nickbrett1/acme.git"


def test_provision_streams_to_the_given_log(rows, monkeypatch):
    def fake_open(repo_url, on_log, **kw):
        on_log("Cloning…")
        return "uri"

    monkeypatch.setattr(actions.opener, "open_repo", fake_open)
    lines = []
    actions.provision("acme", on_log=lines.append)
    assert lines == ["Cloning…"]


def test_provision_without_a_repository_is_an_action_error(rows):
    for empty in ("", "   ", None):
        with pytest.raises(actions.ActionError):
            actions.provision(empty, on_log=lambda line: None)


# -- name resolution --------------------------------------------------------


def test_an_unknown_name_is_an_action_error(rows):
    for verb in (actions.close_project, actions.open_project):
        with pytest.raises(actions.ActionError) as excinfo:
            verb("nope")
        assert "unknown project" in str(excinfo.value)
