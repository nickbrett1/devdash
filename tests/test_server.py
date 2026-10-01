"""The token gate.

The whole point of devdash is that it is reachable from a phone over the
tailnet, so the interesting cases are the *unauthenticated* ones: what leaks,
and what does not.
"""

import http.client
import json
import threading
import time

import pytest

from devdash import actions, jobs, projects, repos, server

TOKEN = "s3cret-token"


@pytest.fixture(autouse=True)
def empty_job_registry():
    """The job registry is module-level state; a leak between tests would make
    a 409 appear out of nowhere."""
    jobs.clear()
    yield
    jobs.clear()


@pytest.fixture
def live(monkeypatch):
    """A real server on an ephemeral port, with /api/* stubbed."""
    monkeypatch.setattr(projects, "rows", lambda: ([{"name": "acme"}], {"window_error": None, "window_titles": 1}))
    monkeypatch.setattr(projects, "status", lambda: {"vm_mem_total": 1})
    httpd = server.serve("127.0.0.1", 0, TOKEN, root=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.Handler.token = ""
        server.Handler.root = None


class Headers(dict):
    """HTTP header names are case-insensitive; http.client preserves whatever
    case the server sent ('Content-type' from the stdlib's own file server)."""

    def __getitem__(self, key):
        return super().__getitem__(key.lower())

    def __contains__(self, key):
        return super().__contains__(key.lower())

    def get(self, key, default=None):
        return super().get(key.lower(), default)


def get(port, path, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("GET", path, headers=headers or {})
        r = conn.getresponse()
        return r.status, Headers((k.lower(), v) for k, v in r.getheaders()), r.read()
    finally:
        conn.close()


def post(port, path, body=None, headers=None):
    payload = None if body is None else json.dumps(body).encode()
    headers = dict(headers or {})
    if payload is not None:
        headers["Content-Type"] = "application/json"
        headers["Content-Length"] = str(len(payload))
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
    try:
        conn.request("POST", path, body=payload, headers=headers)
        r = conn.getresponse()
        return r.status, Headers((k.lower(), v) for k, v in r.getheaders()), r.read()
    finally:
        conn.close()


def test_healthz_is_open_and_reveals_nothing(live):
    status, _headers, body = get(live, "/healthz")
    assert status == 200
    assert json.loads(body) == {"status": "ok"}
    assert "token" not in body.decode()


def test_api_without_a_token_is_401(live):
    status, headers, body = get(live, "/api/projects")
    assert status == 401
    assert headers["WWW-Authenticate"].startswith("Bearer")
    assert json.loads(body) == {"error": "unauthorised"}


def test_api_with_a_bearer_token(live):
    status, _, body = get(live, "/api/projects", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    assert json.loads(body)["projects"] == [{"name": "acme"}]


def test_a_bearer_token_is_compared_whole(live):
    status, _, _ = get(live, "/api/projects", {"Authorization": f"Bearer {TOKEN}x"})
    assert status == 401
    status, _, _ = get(live, "/api/projects", {"Authorization": f"Bearer {TOKEN[:3]}"})
    assert status == 401


def test_first_visit_trades_the_token_for_a_cookie_and_drops_it(live):
    status, headers, _ = get(live, f"/?token={TOKEN}")
    assert status == 303
    # The secret must not survive in the redirect target: that URL is what the
    # phone's browser history and any screenshot will keep.
    assert headers["Location"] == "/"
    assert TOKEN not in headers["Location"]
    cookie = headers["Set-Cookie"]
    assert cookie.startswith(f"{server.COOKIE}={TOKEN}")
    assert "HttpOnly" in cookie
    assert "SameSite=Lax" in cookie


def test_the_cookie_then_authenticates(live):
    _, headers, _ = get(live, f"/api/status?token={TOKEN}")
    cookie = headers["Set-Cookie"].split(";")[0]
    status, _, body = get(live, "/api/status", {"Cookie": cookie})
    assert status == 200
    assert json.loads(body) == {"vm_mem_total": 1}


def test_a_wrong_query_token_is_401_and_sets_nothing(live):
    status, headers, _ = get(live, "/api/projects?token=nope")
    assert status == 401
    assert "Set-Cookie" not in headers


def test_no_token_configured_means_open(monkeypatch):
    """An empty token is an explicit opt-out (used by tests and local dev)."""
    monkeypatch.setattr(projects, "rows", lambda: ([], {"window_error": None, "window_titles": 0}))
    httpd = server.serve("127.0.0.1", 0, "", root=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        status, _, _ = get(httpd.server_address[1], "/api/projects")
        assert status == 200
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.Handler.token = ""
        server.Handler.root = None


# -- the mutating routes ----------------------------------------------------
#
# These are the requests that can stop a container or open a window, so the
# cases that matter are the ones where they must *not*: without a token, and
# without the caller spelling out an override.


def test_post_without_a_token_is_401(live):
    status, _, body = post(live, "/api/projects/acme/close")
    assert status == 401
    assert json.loads(body) == {"error": "unauthorised"}


def test_close_posts_through_to_the_action(live, monkeypatch):
    seen = {}
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: seen.update(name=name, force=force)
                        or {"name": name, "stopped": True, "window_closed": True})
    status, _, body = post(live, "/api/projects/acme/close",
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    assert seen == {"name": "acme", "force": False}
    assert json.loads(body)["stopped"] is True


def test_force_is_taken_from_the_body_and_the_query(live, monkeypatch):
    seen = []
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: seen.append(force) or {"name": name})
    auth = {"Authorization": f"Bearer {TOKEN}"}
    post(live, "/api/projects/acme/close", {"force": True}, auth)
    post(live, "/api/projects/acme/close?force=1", None, auth)
    assert seen == [True, True]


def test_a_refusal_is_a_200_the_ui_can_render(live, monkeypatch):
    """Declining to act is a successful request. A 4xx would tell the browser
    something went wrong when the whole point is that the server did exactly
    what it should."""
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: {"name": name, "refused": True,
                                                   "detail": "live session (tmux: attached)"})
    status, _, body = post(live, "/api/projects/acme/close",
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    payload = json.loads(body)
    assert payload["refused"] is True and "tmux" in payload["detail"]


def test_open_starts_a_job_instead_of_blocking(live, monkeypatch):
    """A first `devcontainer up` takes minutes and a phone's request will not
    wait that long, so open answers 202 with an id to poll."""
    monkeypatch.setattr(actions, "open_project",
                        lambda name, **kw: {"name": name, "uri": "vscode-remote://x"})
    auth = {"Authorization": f"Bearer {TOKEN}"}
    status, _, body = post(live, "/api/projects/acme/open", None, auth)
    assert status == 202
    snapshot = json.loads(body)
    assert snapshot["job_id"] and snapshot["target"] == "acme"

    done = wait_for_job(live, snapshot["job_id"], auth)
    assert done["state"] == "done"
    assert done["result"]["uri"] == "vscode-remote://x"


def wait_for_job(port, job_id, headers, timeout=5.0):
    """Poll a job to completion. The job runs on a thread, so the first poll can
    legitimately still say 'running'."""
    deadline = time.time() + timeout
    while True:
        status, _, body = get(port, f"/api/jobs/{job_id}", headers)
        assert status == 200
        snapshot = json.loads(body)
        if snapshot["state"] != "running" or time.time() > deadline:
            return snapshot
        time.sleep(0.02)


def test_open_passes_fresh_and_clean_only_when_asked(live, monkeypatch):
    seen = []
    monkeypatch.setattr(actions, "open_project",
                        lambda name, fresh=None, clean=None, on_log=None:
                        seen.append((fresh, clean)) or {"name": name, "uri": "uri"})
    auth = {"Authorization": f"Bearer {TOKEN}"}
    wait_for_job(live, json.loads(post(live, "/api/projects/acme/open", None, auth)[2])["job_id"], auth)
    wait_for_job(live, json.loads(post(live, "/api/projects/acme/open", {"fresh": True}, auth)[2])["job_id"], auth)
    assert seen == [(None, None), (True, None)]


def test_a_second_open_for_the_same_target_is_409(live, monkeypatch):
    """Not queued: a second `devcontainer up` on the same workspace would fight
    the first for the same container name."""
    release = threading.Event()
    monkeypatch.setattr(actions, "open_project",
                        lambda name, **kw: (release.wait(5), {"name": name})[1])
    auth = {"Authorization": f"Bearer {TOKEN}"}
    first = json.loads(post(live, "/api/projects/acme/open", None, auth)[2])
    status, _, body = post(live, "/api/projects/acme/open", None, auth)
    assert status == 409
    assert json.loads(body)["job_id"] == first["job_id"]
    release.set()


def test_the_job_log_streams_to_the_poller(live, monkeypatch):
    def open_it(name, on_log=None, **kw):
        on_log("Cloning acme")
        on_log("Container ready")
        return {"name": name}

    monkeypatch.setattr(actions, "open_project", open_it)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    job_id = json.loads(post(live, "/api/projects/acme/open", None, auth)[2])["job_id"]
    assert wait_for_job(live, job_id, auth)["log"] == ["Cloning acme", "Container ready"]


def test_a_job_that_raises_fails_instead_of_hanging(live, monkeypatch):
    def boom(name, **kw):
        raise actions.ActionError("command failed: git clone")

    monkeypatch.setattr(actions, "open_project", boom)
    auth = {"Authorization": f"Bearer {TOKEN}"}
    job_id = json.loads(post(live, "/api/projects/acme/open", None, auth)[2])["job_id"]
    done = wait_for_job(live, job_id, auth)
    assert done["state"] == "failed"
    assert "git clone" in done["error"]


def test_an_unknown_job_is_404(live):
    status, _, _ = get(live, "/api/jobs/nope", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 404


def test_provision_requires_a_repository(live):
    status, _, body = post(live, "/api/provision", {}, {"Authorization": f"Bearer {TOKEN}"})
    assert status == 400
    assert "repository" in json.loads(body)["error"]


def test_provision_starts_a_job_for_the_repo(live, monkeypatch):
    seen = {}
    monkeypatch.setattr(actions, "provision",
                        lambda repo, on_log=None, **kw: seen.update(repo=repo) or {"name": "acme"})
    auth = {"Authorization": f"Bearer {TOKEN}"}
    status, _, body = post(live, "/api/provision", {"repo": "nickbrett1/acme"}, auth)
    assert status == 202
    done = wait_for_job(live, json.loads(body)["job_id"], auth)
    assert done["target"] == "nickbrett1/acme"
    assert seen == {"repo": "nickbrett1/acme"}


def test_repos_lists_what_has_no_workspace_yet(live, monkeypatch):
    monkeypatch.setattr(repos, "workspaces_dir", lambda: "/w")
    monkeypatch.setattr(repos, "unprovisioned",
                        lambda ws, **kw: (["nickbrett1/greenfield"], "listing is stale"))
    status, _, body = get(live, "/api/repos", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    payload = json.loads(body)
    assert payload["repos"] == ["nickbrett1/greenfield"]
    # A listing failure is a message beside the answer, not a 500 — the picker
    # still has something to show.
    assert payload["error"] == "listing is stale"


def test_an_action_error_is_a_400(live, monkeypatch):
    def boom(name, force=False):
        raise actions.ActionError("unknown project 'nope'")

    monkeypatch.setattr(actions, "close_project", boom)
    status, _, body = post(live, "/api/projects/nope/close",
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 400
    assert "unknown project" in json.loads(body)["error"]


def test_an_unexpected_action_failure_is_a_500(live, monkeypatch):
    def boom(name, force=False):
        raise RuntimeError("docker died")

    monkeypatch.setattr(actions, "close_project", boom)
    status, _, body = post(live, "/api/projects/acme/close",
                           headers={"Authorization": f"Bearer {TOKEN}"})
    assert status == 500
    assert "docker died" in json.loads(body)["error"]


def test_unknown_routes_are_404(live):
    auth = {"Authorization": f"Bearer {TOKEN}"}
    assert post(live, "/api/projects/acme", None, auth)[0] == 404
    assert post(live, "/api/projects/acme/destroy", None, auth)[0] == 404
    assert post(live, "/api/projects", None, auth)[0] == 404


def test_a_broken_join_is_a_500_not_a_dead_server(live, monkeypatch):
    def boom():
        raise RuntimeError("docker is not reachable")

    monkeypatch.setattr(projects, "rows", boom)
    status, _, body = get(live, "/api/projects", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 500
    assert "docker is not reachable" in json.loads(body)["error"]


def test_the_frontend_is_behind_the_token_too(live):
    """Not just /api/*: an unauthenticated visitor gets nothing at all."""
    assert get(live, "/")[0] == 401


def test_unbuilt_frontend_says_so_instead_of_404ing(live):
    status, headers, body = get(live, "/", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 200
    assert headers["Content-Type"].startswith("text/plain")
    assert b"npm run build" in body


def test_static_files_come_from_web_dir_not_the_cwd(tmp_path, monkeypatch):
    """SimpleHTTPRequestHandler defaults to the cwd, which under launchd is /.
    Serving the repository root to the tailnet would be a fine way to leak
    .git, .devcontainer and every other checkout on the machine."""
    monkeypatch.setattr(projects, "rows", lambda: ([], {}))
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<h1>devdash</h1>")
    (tmp_path / "secret.txt").write_text("do not serve me")

    httpd = server.serve("127.0.0.1", 0, TOKEN, root=dist)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        port = httpd.server_address[1]
        status, headers, body = get(port, "/", {"Authorization": f"Bearer {TOKEN}"})
        assert status == 200
        assert headers["Content-Type"].startswith("text/html")
        assert b"devdash" in body
        assert get(port, "/secret.txt", {"Authorization": f"Bearer {TOKEN}"})[0] == 404
    finally:
        httpd.shutdown()
        httpd.server_close()
        server.Handler.token = ""
        server.Handler.root = None
