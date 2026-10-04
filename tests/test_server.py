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
    monkeypatch.setattr(projects, "rows",
                        lambda **kw: ([{"name": "acme"}], {"window_error": None, "window_titles": 1}))
    monkeypatch.setattr(projects, "status", lambda: {"vm_mem_total": 1})
    # The connection probe and the stats sample are real `docker` calls; this is
    # not the test for them.
    monkeypatch.setattr(projects, "connections", dict)
    monkeypatch.setattr(projects, "memory", dict)
    # serve() warms the read caches in a thread; that is real docker, and this
    # is not the test for it.
    monkeypatch.setattr(projects, "warm", lambda: None)
    httpd = server.serve("127.0.0.1", 0, root=None)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
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









# -- the mutating routes ----------------------------------------------------
#
# These are the requests that can stop a container or open a window, so the
# cases that matter are the ones where they must *not*: without a token, and
# without the caller spelling out an override.



def test_close_posts_through_to_the_action(live, monkeypatch):
    seen = {}
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: seen.update(name=name, force=force)
                        or {"name": name, "stopped": True, "window_closed": True})
    status, _, body = post(live, "/api/projects/acme/close",
                           )
    assert status == 200
    assert seen == {"name": "acme", "force": False}
    assert json.loads(body)["stopped"] is True


def test_a_close_drops_the_cached_samples(live, monkeypatch):
    """The phone reloads the moment this returns, and the page is built from a
    sample of the container list the close just changed."""
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: {"name": name, "stopped": True})
    dropped = []
    monkeypatch.setattr(projects, "invalidate", lambda: dropped.append(1))

    post(live, "/api/projects/acme/close")

    assert dropped == [1]


def test_tailscale_registration_posts_through_and_answers_with_the_url(live, monkeypatch):
    """The registration now waits for the login, so it is a job; the URL rides
    on the job as `attention` for the phone to tap."""
    monkeypatch.setattr(actions, "register_tailscale",
                        lambda name, **kw: {"name": name, "started": True,
                                            "url": "https://login.tailscale.com/a/x",
                                            "detail": "authenticate"})
    status, _, body = post(live, "/api/projects/acme/tailscale")
    # The registration waits for the browser login, so like open/provision it is
    # a job; the URL rides on the job for the phone to tap.
    assert status == 202
    done = wait_for_job(live, json.loads(body)["job_id"])
    assert done["result"]["url"].startswith("https://login.tailscale.com/")


def test_a_tailscale_registration_drops_the_cached_samples(live, monkeypatch):
    monkeypatch.setattr(actions, "register_tailscale", lambda name: {"name": name})
    dropped = []
    monkeypatch.setattr(projects, "invalidate", lambda: dropped.append(1))

    post(live, "/api/projects/acme/tailscale")

    assert dropped == [1]


def test_a_finished_job_drops_the_cached_samples(live, monkeypatch):
    """A `devcontainer up` that has finished is exactly when the list the next
    poll shows becomes wrong — including when it fails half a container up."""
    monkeypatch.setattr(actions, "open_project", lambda name, **kw: {"name": name})
    dropped = []
    monkeypatch.setattr(projects, "invalidate", lambda: dropped.append(1))

    wait_for_job(live, json.loads(post(live, "/api/projects/acme/open", None)[2])["job_id"])

    assert dropped == [1]


def test_serve_warms_the_reads_off_the_request_path(monkeypatch):
    """`docker stats` is ~2s; a restart should not make the next poll pay for
    the first sample while someone watches a spinner."""
    warmed = threading.Event()
    monkeypatch.setattr(projects, "warm", warmed.set)

    httpd = server.serve("127.0.0.1", 0, root=None)
    try:
        assert warmed.wait(5), "serve() should have started the warmup"
    finally:
        httpd.server_close()


def test_force_is_taken_from_the_body_and_the_query(live, monkeypatch):
    seen = []
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: seen.append(force) or {"name": name})
    post(live, "/api/projects/acme/close", {"force": True})
    post(live, "/api/projects/acme/close?force=1", None)
    assert seen == [True, True]


def test_a_refusal_is_a_200_the_ui_can_render(live, monkeypatch):
    """Declining to act is a successful request. A 4xx would tell the browser
    something went wrong when the whole point is that the server did exactly
    what it should."""
    monkeypatch.setattr(actions, "close_project",
                        lambda name, force=False: {"name": name, "refused": True,
                                                   "detail": "live session (tmux: attached)"})
    status, _, body = post(live, "/api/projects/acme/close",
                           )
    assert status == 200
    payload = json.loads(body)
    assert payload["refused"] is True and "tmux" in payload["detail"]


def test_open_starts_a_job_instead_of_blocking(live, monkeypatch):
    """A first `devcontainer up` takes minutes and a phone's request will not
    wait that long, so open answers 202 with an id to poll."""
    monkeypatch.setattr(actions, "open_project",
                        lambda name, **kw: {"name": name, "uri": "vscode-remote://x"})
    status, _, body = post(live, "/api/projects/acme/open", None)
    assert status == 202
    snapshot = json.loads(body)
    assert snapshot["job_id"] and snapshot["target"] == "acme"

    done = wait_for_job(live, snapshot["job_id"])
    assert done["state"] == "done"
    assert done["result"]["uri"] == "vscode-remote://x"


def wait_for_job(port, job_id, timeout=5.0):
    """Poll a job to completion. The job runs on a thread, so the first poll can
    legitimately still say 'running'."""
    deadline = time.time() + timeout
    while True:
        status, _, body = get(port, f"/api/jobs/{job_id}")
        assert status == 200
        snapshot = json.loads(body)
        if snapshot["state"] != "running" or time.time() > deadline:
            return snapshot
        time.sleep(0.02)


def test_open_passes_fresh_and_clean_only_when_asked(live, monkeypatch):
    seen = []
    monkeypatch.setattr(actions, "open_project",
                        lambda name, fresh=None, clean=None, on_log=None, **kw:
                        seen.append((fresh, clean)) or {"name": name, "uri": "uri"})
    wait_for_job(live, json.loads(post(live, "/api/projects/acme/open", None)[2])["job_id"])
    wait_for_job(live, json.loads(post(live, "/api/projects/acme/open", {"fresh": True})[2])["job_id"])
    assert seen == [(None, None), (True, None)]


def test_a_second_open_for_the_same_target_is_409(live, monkeypatch):
    """Not queued: a second `devcontainer up` on the same workspace would fight
    the first for the same container name."""
    release = threading.Event()
    monkeypatch.setattr(actions, "open_project",
                        lambda name, **kw: (release.wait(5), {"name": name})[1])
    first = json.loads(post(live, "/api/projects/acme/open", None)[2])
    status, _, body = post(live, "/api/projects/acme/open", None)
    assert status == 409
    assert json.loads(body)["job_id"] == first["job_id"]
    release.set()


def test_the_job_log_streams_to_the_poller(live, monkeypatch):
    def open_it(name, on_log=None, **kw):
        on_log("Cloning acme")
        on_log("Container ready")
        return {"name": name}

    monkeypatch.setattr(actions, "open_project", open_it)
    job_id = json.loads(post(live, "/api/projects/acme/open", None)[2])["job_id"]
    assert wait_for_job(live, job_id)["log"] == ["Cloning acme", "Container ready"]


def test_a_job_that_raises_fails_instead_of_hanging(live, monkeypatch):
    def boom(name, **kw):
        raise actions.ActionError("command failed: git clone")

    monkeypatch.setattr(actions, "open_project", boom)
    job_id = json.loads(post(live, "/api/projects/acme/open", None)[2])["job_id"]
    done = wait_for_job(live, job_id)
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
    status, _, body = post(live, "/api/provision", {"repo": "nickbrett1/acme"})
    assert status == 202
    done = wait_for_job(live, json.loads(body)["job_id"])
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
                           )
    assert status == 400
    assert "unknown project" in json.loads(body)["error"]


def test_an_unexpected_action_failure_is_a_500(live, monkeypatch):
    def boom(name, force=False):
        raise RuntimeError("docker died")

    monkeypatch.setattr(actions, "close_project", boom)
    status, _, body = post(live, "/api/projects/acme/close",
                           )
    assert status == 500
    assert "docker died" in json.loads(body)["error"]


def test_unknown_routes_are_404(live):
    assert post(live, "/api/projects/acme", None)[0] == 404
    assert post(live, "/api/projects/acme/destroy", None)[0] == 404
    assert post(live, "/api/projects", None)[0] == 404


def test_projects_carries_the_samples_to_the_rows(live, monkeypatch):
    """The per-row figures come from the same stats sample the strip shows, and
    the connection facts from the tailnet probe, so the route has to hand both
    to the join rather than making the join pay for them per row."""
    seen = {}
    monkeypatch.setattr(projects, "memory", lambda: {"c1": 42})
    monkeypatch.setattr(projects, "connections", lambda: {"c1": {"state": "connected"}})
    monkeypatch.setattr(projects, "rows",
                        lambda **kw: (seen.update(kw) or [{"name": "acme"}], {"window_titles": 1}))

    status, _, _body = get(live, "/api/projects")

    assert status == 200
    assert seen["memory"] == {"c1": 42}
    assert seen["connections"] == {"c1": {"state": "connected"}}


def test_a_broken_join_is_a_500_not_a_dead_server(live, monkeypatch):
    def boom(**kw):
        raise RuntimeError("docker is not reachable")

    monkeypatch.setattr(projects, "rows", boom)
    status, _, body = get(live, "/api/projects", {"Authorization": f"Bearer {TOKEN}"})
    assert status == 500
    assert "docker is not reachable" in json.loads(body)["error"]


def test_everything_is_open_because_the_tailnet_is_the_gate(live):
    """There is no token: the bind address is the access control. The one thing
    worth asserting is that nothing *creates* a credential any more — a server
    that still set a cookie would imply a gate that is not there."""
    for path in ("/", "/api/projects", "/api/status", "/healthz", "/nothing-here"):
        _status, headers, _body = get(live, path)
        assert "Set-Cookie" not in headers, path
    assert get(live, "/api/projects")[0] == 200


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

    httpd = server.serve("127.0.0.1", 0, root=dist)
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
        server.Handler.root = None
