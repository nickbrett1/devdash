"""Open and close — the only mutating code in devdash.

Synthetic names (acme, example-one) throughout: this repo is public, and a
fixture that named a real workspace would publish it. What is being tested is
the *policy* — what devdash refuses, what it passes on, and what it does to the
two halves of Close when only one of them can succeed.
"""

import subprocess

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
    # The tailscale gate looks the container up in docker directly; by default a
    # test has no container, so the gate is a no-op and open/provision results
    # are unchanged. A test that wants the gate shapes this list.
    monkeypatch.setattr(actions.containers, "list_devcontainers", list)
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


# -- tailscale registration --------------------------------------------------


def test_register_tailscale_registers_waits_and_starts_the_agent(rows, monkeypatch):
    """The manual retry now stands still for the login and re-runs the hook, so
    it also fixes a container that was already on the tailnet but whose agent
    never came up."""
    monkeypatch.setattr(actions.containers, "list_devcontainers", lambda: [_container()])
    monkeypatch.setattr(actions.tailnet, "facts",
                        lambda cid: {"state": "logged_out", "ip": None})
    monkeypatch.setattr(actions.tailnet, "register",
                        lambda cid, host: {"started": True, "url": "https://login.tailscale.com/a/x",
                                           "detail": "authenticate"})
    monkeypatch.setattr(actions.tailnet, "wait_connected", lambda cid, timeout, **kw: True)
    monkeypatch.setattr(actions, "start_agent", lambda cid, ws: (True, "ok"))
    out = actions.register_tailscale("acme")
    assert out["name"] == "acme" and out["started"] is True
    assert out["state"] == "connected" and out["agent_started"] is True
    assert out["url"].startswith("https://login.tailscale.com/")


def test_register_tailscale_refuses_a_container_that_is_not_running(rows):
    rows["row"] = _row(state="stopped")
    with pytest.raises(actions.ActionError) as excinfo:
        actions.register_tailscale("acme")
    assert "not running" in str(excinfo.value)


# -- the tailscale gate that open/provision run ------------------------------
#
# This is the step the CLI takes mid-open and devdash used to skip without an
# authkey. It must stop the job, hand the phone the login URL, wait, and only
# then re-run the post-start hook so the container agent starts with an address.


def _container(**over):
    c = {"id": "acme-dev", "name": "acme-dev", "workspace": "/w/acme", "running": True}
    c.update(over)
    return c


def test_gate_is_a_noop_without_a_container(rows):
    assert actions.tailscale_gate("acme", lambda line: None) is None


def test_gate_is_a_noop_when_already_connected(rows, monkeypatch):
    monkeypatch.setattr(actions.containers, "list_devcontainers", lambda: [_container()])
    monkeypatch.setattr(actions.tailnet, "facts",
                        lambda cid: {"state": "connected", "ip": "100.0.0.1"})
    assert actions.tailscale_gate("acme", lambda line: None) is None


def test_gate_registers_waits_then_starts_the_agent(rows, monkeypatch):
    monkeypatch.setattr(actions.containers, "list_devcontainers", lambda: [_container()])
    monkeypatch.setattr(actions.tailnet, "facts",
                        lambda cid: {"state": "logged_out", "ip": None})
    monkeypatch.setattr(actions.tailnet, "register",
                        lambda cid, host: {"started": True, "detail": "authenticate",
                                           "url": "https://login.tailscale.com/a/x"})
    monkeypatch.setattr(actions.tailnet, "wait_connected", lambda cid, timeout, **kw: True)
    started = {}
    monkeypatch.setattr(actions, "start_agent",
                        lambda cid, ws: started.update(cid=cid, ws=ws) or (True, "ok"))
    seen = {}
    out = actions.tailscale_gate(
        "acme",
        lambda line: seen.setdefault("log", []).append(line),
        lambda payload: seen.update(attention=payload),
    )
    assert out["state"] == "connected"
    assert out["connected"] is True and out["agent_started"] is True
    assert started == {"cid": "acme-dev", "ws": "/w/acme"}
    # The URL reaches the caller as structured state, not only as a log line, so
    # the phone can render a tap target.
    assert seen["attention"]["kind"] == "tailscale"
    assert seen["attention"]["url"].startswith("https://login.tailscale.com/")
    assert any("login.tailscale.com" in line for line in seen["log"])


def test_gate_reports_a_timeout_without_touching_the_agent(rows, monkeypatch):
    monkeypatch.setattr(actions.containers, "list_devcontainers", lambda: [_container()])
    monkeypatch.setattr(actions.tailnet, "facts",
                        lambda cid: {"state": "logged_out", "ip": None})
    monkeypatch.setattr(actions.tailnet, "register",
                        lambda cid, host: {"started": True, "detail": "authenticate",
                                           "url": "https://login.tailscale.com/a/x"})
    monkeypatch.setattr(actions.tailnet, "wait_connected", lambda cid, timeout, **kw: False)

    def must_not_start(*a, **kw):
        raise AssertionError("the agent must not start before the container is on the tailnet")

    monkeypatch.setattr(actions, "start_agent", must_not_start)
    out = actions.tailscale_gate("acme", lambda line: None)
    assert out["state"] == "logged_out"
    assert out["connected"] is False and "agent_started" not in out


def test_open_project_carries_the_tailscale_result(rows, monkeypatch):
    monkeypatch.setattr(actions.opener, "open_repo", lambda repo_url, **kw: "uri")
    monkeypatch.setattr(actions, "tailscale_gate",
                        lambda name, on_log, on_attention=None:
                        {"state": "logged_out", "url": "https://login.tailscale.com/a/x"})
    out = actions.open_project("acme")
    assert out["tailscale"]["url"].startswith("https://login.tailscale.com/")


def test_provision_carries_the_tailscale_result(rows, monkeypatch):
    monkeypatch.setattr(actions.opener, "open_repo", lambda repo_url, **kw: "uri")
    monkeypatch.setattr(actions, "tailscale_gate",
                        lambda name, on_log, on_attention=None: {"state": "connected"})
    out = actions.provision("acme", on_log=lambda line: None)
    assert out["tailscale"] == {"state": "connected"}


def test_start_agent_reruns_the_hook_as_the_container_user(monkeypatch, tmp_path):
    """The hook must run as the container's own user with the right HOME — this
    repo's is `node`, and `docker exec -u vscode` would simply fail. With no
    `remoteUser` in the config, the container's own `Config.User` is the source."""
    ws = tmp_path / "acme"
    (ws / ".devcontainer").mkdir(parents=True)
    (ws / ".devcontainer" / "devcontainer.json").write_text(
        '{"workspaceFolder": "/workspaces/acme", '
        '"postStartCommand": "bash /workspaces/acme/.devcontainer/post-start-setup.sh"}')
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "node\n", "")
        if argv[2:4] == ["-u", "root"] and "getent passwd" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, "/home/node\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    ok, detail = actions.start_agent("acme-dev", str(ws))
    assert ok is True and "post-start" in detail
    hook = calls[-1]
    assert hook[2:4] == ["-u", "node"]
    assert "HOME=/home/node" in hook
    assert hook[hook.index("-w") + 1] == "/workspaces/acme"
    assert hook[-3:] == ["sh", "-lc",
                         "bash /workspaces/acme/.devcontainer/post-start-setup.sh"]


def test_container_user_falls_back_to_the_workspace_owner(monkeypatch, tmp_path):
    """When the container's config names no user, the workspace owner is the last
    guess (`stat`). A root-owned workspace here means root, not a crash."""
    ws = tmp_path / "acme"
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        if "inspect" in argv:
            return subprocess.CompletedProcess(argv, 0, "\n", "")
        if "stat -c %U" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, "vscode\n", "")
        if "getent passwd" in argv[-1]:
            return subprocess.CompletedProcess(argv, 0, "/home/vscode\n", "")
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    assert actions._container_user("acme-dev", {"workspaceFolder": "/workspaces/acme"}, str(ws)) == (
        "vscode",
        "/home/vscode",
    )


def test_container_user_prefers_remote_user_over_the_container(monkeypatch, tmp_path):
    """An explicit `remoteUser` in the config wins; we do not even inspect."""
    calls = []

    def fake_run(argv, **kw):
        calls.append(argv)
        assert "inspect" not in argv
        return subprocess.CompletedProcess(argv, 0, "/home/node\n", "")

    monkeypatch.setattr(actions.subprocess, "run", fake_run)
    assert actions._container_user("acme-dev", {"remoteUser": "node"}, str(tmp_path / "acme")) == (
        "node",
        "/home/node",
    )


def test_start_agent_reports_a_failed_hook(monkeypatch, tmp_path):
    ws = tmp_path / "acme"
    (ws / ".devcontainer").mkdir(parents=True)
    (ws / ".devcontainer" / "devcontainer.json").write_text('{"postStartCommand": "boom"}')
    monkeypatch.setattr(actions.subprocess, "run",
                        lambda argv, **kw: subprocess.CompletedProcess(argv, 3, "", "agent: no address"))
    ok, detail = actions.start_agent("acme-dev", str(ws))
    assert ok is False
    assert "exited 3" in detail and "no address" in detail


def test_start_agent_without_a_hook_is_reported_not_guessed(tmp_path):
    (tmp_path / ".devcontainer").mkdir()
    (tmp_path / ".devcontainer" / "devcontainer.json").write_text("{}")
    ok, detail = actions.start_agent("acme-dev", str(tmp_path))
    assert ok is False and "no postStartCommand" in detail


def test_post_start_command_reads_a_string_and_an_array(tmp_path):
    config = tmp_path / ".devcontainer"
    config.mkdir()
    (config / "devcontainer.json").write_text('{"postStartCommand": "echo hi"}')
    assert actions._post_start_command(actions._devcontainer_config(str(tmp_path))) == ["sh", "-lc", "echo hi"]

    (config / "devcontainer.json").write_text('{"postStartCommand": ["echo", "hi"]}')
    assert actions._post_start_command(actions._devcontainer_config(str(tmp_path))) == ["echo", "hi"]

    (config / "devcontainer.json").write_text("{}")
    assert actions._post_start_command(actions._devcontainer_config(str(tmp_path))) is None
    assert actions._post_start_command(actions._devcontainer_config("/nonexistent")) is None


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
