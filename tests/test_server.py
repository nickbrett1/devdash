"""The token gate.

The whole point of devdash is that it is reachable from a phone over the
tailnet, so the interesting cases are the *unauthenticated* ones: what leaks,
and what does not.
"""

import http.client
import json
import threading

import pytest

from devdash import projects, server

TOKEN = "s3cret-token"


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
