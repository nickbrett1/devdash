"""The MCP surface: an agent's view of the same actions the phone calls.

Unit-level — `mcp.handle` is called directly, no socket. The integration
(transport) cases live in test_server.py beside the other route tests.
"""

import threading
import time

import pytest

from devdash import actions, jobs, mcp, projects


@pytest.fixture(autouse=True)
def empty_job_registry():
    """The job registry is module-level state; a leak between tests would make
    a job out of nowhere change a `start_unique` answer."""
    jobs.clear()
    yield
    jobs.clear()


CFG = {"mcp": {"enabled": True, "tools": []}}


def call(message, cfg=None):
    return mcp.handle(message, cfg or CFG)


def rpc(method, params=None, rid=1):
    message = {"jsonrpc": "2.0", "id": rid, "method": method}
    if params is not None:
        message["params"] = params
    return message


def tool_call(name, arguments, cfg=None):
    return call(rpc("tools/call", {"name": name, "arguments": arguments}), cfg)


def spec(name, cfg=None):
    tools = call(rpc("tools/list"), cfg)["result"]["tools"]
    return next(t for t in tools if t["name"] == name)


# -- protocol ------------------------------------------------------------------


def test_initialize_advertises_tools():
    result = call(rpc("initialize", {"protocolVersion": "2025-06-18"}))["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"]["tools"]["listChanged"] is False
    assert result["serverInfo"]["name"] == "devdash"


def test_initialize_falls_back_on_an_unknown_version():
    result = call(rpc("initialize", {"protocolVersion": "1999-01-01"}))["result"]
    assert result["protocolVersion"] == mcp.PROTOCOL_VERSION


def test_an_unknown_method_is_method_not_found():
    error = call(rpc("no/such/method"))["error"]
    assert error["code"] == -32601


def test_a_request_without_a_method_is_invalid():
    error = call({"jsonrpc": "2.0", "id": 1})["error"]
    assert error["code"] == -32600


def test_a_notification_has_no_reply():
    assert call({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_ping_is_answered():
    assert call(rpc("ping"))["result"] == {}


# -- tools/list ----------------------------------------------------------------


def test_the_default_set_includes_rebuild_and_excludes_provision():
    names = {t["name"] for t in call(rpc("tools/list"))["result"]["tools"]}
    assert "rebuild_project" in names
    assert "open_project" in names
    assert "close_project" in names
    assert "provision_project" not in names


def test_annotations_are_honest():
    assert spec("list_projects")["annotations"]["readOnlyHint"] is True
    # MCP defaults destructiveHint to *true*; `open` must say otherwise.
    assert spec("open_project")["annotations"]["destructiveHint"] is False
    assert spec("rebuild_project")["annotations"]["destructiveHint"] is True
    assert spec("close_project")["annotations"]["destructiveHint"] is True


def test_an_allowlist_replaces_the_default_set():
    cfg = {"mcp": {"enabled": True, "tools": ["list_projects"]}}
    names = {t["name"] for t in call(rpc("tools/list"), cfg)["result"]["tools"]}
    assert names == {"list_projects"}


def test_provision_is_reachable_only_by_naming_it():
    cfg = {"mcp": {"enabled": True, "tools": ["provision_project"]}}
    names = {t["name"] for t in call(rpc("tools/list"), cfg)["result"]["tools"]}
    assert names == {"provision_project"}


# -- tools/call ----------------------------------------------------------------


def test_an_unknown_tool_is_invalid_params():
    assert tool_call("no_such_tool", {})["error"]["code"] == -32602


def test_a_hidden_tool_is_not_callable():
    assert tool_call("provision_project", {"repo": "o/r"})["error"]["code"] == -32602


def test_a_missing_required_argument_is_invalid_params():
    assert tool_call("open_project", {})["error"]["code"] == -32602


def test_open_starts_a_job(monkeypatch):
    seen = {}
    monkeypatch.setattr(actions, "open_project",
                        lambda name, fresh=None, **kw: seen.update(name=name, fresh=fresh)
                        or {"uri": "vscode://x"})
    result = tool_call("open_project", {"name": "acme"})["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["job_id"]
    assert result["structuredContent"]["target"] == "acme"
    assert seen == {"name": "acme", "fresh": False}


def test_rebuild_asks_for_fresh(monkeypatch):
    seen = {}
    monkeypatch.setattr(actions, "open_project",
                        lambda name, fresh=None, **kw: seen.update(fresh=fresh) or {})
    tool_call("rebuild_project", {"name": "acme"})
    assert seen == {"fresh": True}


def test_a_second_open_reuses_the_running_job(monkeypatch):
    """Two opens for one target must not fight for the same container name —
    the second follows the first, mirroring the HTTP route's 409."""
    release = threading.Event()
    monkeypatch.setattr(actions, "open_project",
                        lambda name, **kw: release.wait(2) or {"uri": "x"})
    first = tool_call("open_project", {"name": "acme"})["result"]["structuredContent"]
    second = tool_call("open_project", {"name": "acme"})["result"]["structuredContent"]
    assert second["job_id"] == first["job_id"]
    release.set()


def test_a_close_refusal_is_an_error(monkeypatch):
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: {"name": name, "refused": True,
                                                   "stopped": False, "detail": "live session"})
    result = tool_call("close_project", {"name": "acme"})["result"]
    assert result["isError"] is True
    assert result["structuredContent"]["refused"] is True


def test_a_successful_close_is_not_an_error(monkeypatch):
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: {"name": name, "stopped": True,
                                                   "window_closed": True})
    result = tool_call("close_project", {"name": "acme"})["result"]
    assert result["isError"] is False
    assert result["structuredContent"]["stopped"] is True


def test_a_raising_action_becomes_an_error_result(monkeypatch):
    def boom(name, force=False):
        raise actions.ActionError("unknown project 'acme'")

    monkeypatch.setattr(actions, "close_project", boom)
    result = tool_call("close_project", {"name": "acme"})["result"]
    assert result["isError"] is True
    assert "unknown project" in result["content"][0]["text"]


def test_a_failed_job_reads_as_an_error():
    def boom(on_log, on_attention):
        raise actions.ActionError("boom")

    snapshot = jobs.start("open", "acme", boom)
    time.sleep(0.05)
    result = tool_call("get_job", {"job_id": snapshot["job_id"]})["result"]
    assert result["isError"] is True


def test_an_unknown_job_reads_as_an_error():
    result = tool_call("get_job", {"job_id": "nope"})["result"]
    assert result["isError"] is True


def test_list_projects_returns_the_rows(monkeypatch):
    monkeypatch.setattr(projects, "rows",
                        lambda **kw: ([{"name": "acme", "state": "running"}],
                                      {"window_error": None, "screen_locked": False}))
    result = tool_call("list_projects", {}, CFG)["result"]
    assert result["structuredContent"]["projects"][0]["name"] == "acme"
