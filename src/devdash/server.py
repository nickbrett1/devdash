"""devdash's HTTP surface: a small JSON API, and the built SvelteKit app.

Standard library only. The generated harness was stdlib and four routes do not
justify a framework — and every dependency here is one more thing that has to
be installed before the thing that *stops memory-hungry containers* will run.

Two rules this file exists to enforce:

  * nothing mutating is reachable without the token, and nothing mutating is a
    GET (M2's open/close arrive as POSTs);
  * the server never listens on 0.0.0.0 by default — see config.bind_host.
"""

import hmac
import json
import secrets
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import config, projects

COOKIE = "devdash_token"
HEALTH_BODY = b'{"status": "ok"}'


def web_root(cfg):
    """Where the SvelteKit build landed. Resolved against the repo root, not
    the cwd, because launchd starts the server from /."""
    root = Path(cfg.get("web_dir") or "web/dist")
    if not root.is_absolute():
        root = Path(__file__).resolve().parents[2] / root
    return root


class Handler(SimpleHTTPRequestHandler):
    """JSON API + static files, behind one shared secret."""

    token = ""
    root = None

    # -- helpers ----------------------------------------------------------

    def _send_json(self, payload, status=200):
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE:
                return value
        return ""

    def _authorised(self):
        """Bearer header, cookie, or ?token= (the phone's first visit)."""
        if not self.token:
            return True
        auth = self.headers.get("Authorization") or ""
        candidates = []
        if auth.startswith("Bearer "):
            candidates.append(auth[len("Bearer "):])
        candidates.append(self._cookie_token())
        candidates += parse_qs(urlparse(self.path).query).get("token", [])
        return any(c and hmac.compare_digest(c, self.token) for c in candidates)

    def _unauthorised(self):
        body = b'{"error": "unauthorised"}'
        self.send_response(401)
        self.send_header("Content-Type", "application/json")
        self.send_header("WWW-Authenticate", 'Bearer realm="devdash"')
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    # -- routes -----------------------------------------------------------

    def do_GET(self):
        path = urlparse(self.path).path

        # Unauthenticated on purpose: the LaunchAgent and any probe need a
        # cheap liveness answer, and it reveals nothing.
        if path == "/healthz":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(HEALTH_BODY)))
            self.end_headers()
            self.wfile.write(HEALTH_BODY)
            return

        if not self._authorised():
            self._unauthorised()
            return

        # Trade ?token=… for a cookie and drop it from the URL, so the secret
        # does not linger in phone browser history or a shared link.
        if "token" in parse_qs(urlparse(self.path).query):
            self.send_response(303)
            self.send_header("Location", path or "/")
            self.send_header("Set-Cookie", f"{COOKIE}={self.token}; HttpOnly; SameSite=Lax; Path=/")
            self.end_headers()
            return

        try:
            if path == "/api/projects":
                rows, meta = projects.rows()
                self._send_json({"projects": rows, **meta})
                return
            if path == "/api/status":
                self._send_json(projects.status())
                return
        except Exception as e:  # a broken join must not look like a dead server
            self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
            return

        self._serve_static()

    def _serve_static(self):
        if self.root is None or not self.root.is_dir():
            body = (
                "devdash API is up, but the frontend has not been built.\n\n"
                "  cd web && npm install && npm run build\n\n"
                "API: /api/projects, /api/status, /healthz\n"
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def log_message(self, fmt, *args):
        """One line per request on stderr, into the LaunchAgent's log."""


def serve(host, port, token, root):
    Handler.token = token or ""
    Handler.root = root
    # Threading matters: /api/projects shells out to docker per project, and a
    # single-threaded server would freeze the phone on the first poll.
    httpd = ThreadingHTTPServer((host, port), Handler)
    httpd.daemon_threads = True
    return httpd
