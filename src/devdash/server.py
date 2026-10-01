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
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from . import actions, jobs, projects, repos

COOKIE = "devdash_token"
HEALTH_BODY = b'{"status": "ok"}'

_TRUTHY = ("1", "true", "on", "yes")


def _flag(key, body, query, default=False):
    """A boolean request parameter, from the JSON body or the query string.

    Absent means `default` — for `force` that is False, which is the whole
    point: the live-session veto is what happens when nobody said otherwise.
    """
    if key in body:
        value = body[key]
        return value if isinstance(value, bool) else str(value).lower() in _TRUTHY
    if key in query:
        return str(query[key][0]).lower() in _TRUTHY
    return default


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
            if path == "/api/repos":
                available, listing_error = repos.unprovisioned(repos.workspaces_dir())
                self._send_json({"repos": available, "error": listing_error})
                return
            if path.startswith("/api/jobs/"):
                job = jobs.get(path.rsplit("/", 1)[-1])
                if job is None:
                    self._send_json({"error": "no such job"}, status=404)
                else:
                    self._send_json(job)
                return
            if path == "/api/jobs":
                self._send_json({"jobs": jobs.running()})
                return
        except Exception as e:  # noqa: BLE001 — a broken join must not look like a dead server
            self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
            return

        self._serve_static()

    def do_POST(self):
        """The only mutating surface, and therefore never reachable without
        the token and never a GET: a link prefetcher or a phone's back button
        must not be able to stop a container."""
        if not self._authorised():
            self._unauthorised()
            return

        parsed = urlparse(self.path)
        path = parsed.path.rstrip("/")
        parts = [unquote(p) for p in path.split("/") if p]
        body = self._json_body()
        query = parse_qs(parsed.query)

        try:
            # /api/projects/<name>/close — fast (a docker stop and an osascript),
            # so it answers with the outcome rather than a job id.
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "close":
                result = actions.close_project(parts[2], force=_flag("force", body, query))
                self._send_json(result)
                return

            # /api/projects/<name>/open and /api/provision — potentially minutes
            # of `devcontainer up`, so these answer 202 with a job id and the
            # caller polls /api/jobs/<id> for the log.
            if len(parts) == 4 and parts[:2] == ["api", "projects"] and parts[3] == "open":
                self._start("open", parts[2], body, query,
                            lambda on_log, **kw: actions.open_project(parts[2], on_log=on_log, **kw))
                return

            if parts == ["api", "provision"]:
                repo = str(body.get("repo") or query.get("repo", [""])[0]).strip()
                if not repo:
                    self._send_json({"error": "a repository is required"}, status=400)
                    return
                self._start("provision", repo, body, query,
                            lambda on_log, **kw: actions.provision(repo, on_log=on_log, **kw))
                return
        except actions.ActionError as e:
            self._send_json({"error": str(e)}, status=400)
            return
        except Exception as e:  # noqa: BLE001 — a crashed action is a 500, not a dead server
            self._send_json({"error": f"{type(e).__name__}: {e}"}, status=500)
            return

        self._send_json({"error": "not found"}, status=404)

    def _start(self, kind, target, body, query, fn):
        """Begin a job unless one for the same target is already running.

        Refusing rather than queueing is the honest answer: `devcontainer up`
        on a workspace that is already building would fight the first one for
        the same container name, and the phone would rather hear "already
        running" than wait behind a build it did not start.
        """
        for existing in jobs.running(kind=kind, target=target):
            self._send_json({"error": f"{kind} already running for {target}",
                             "job_id": existing["job_id"], "state": existing["state"]},
                            status=409)
            return
        snapshot = jobs.start(kind, target, lambda on_log: fn(
            on_log=on_log,
            fresh=_flag("fresh", body, query, default=None),
            clean=_flag("clean", body, query, default=None),
        ))
        self._send_json(snapshot, status=202)

    def _json_body(self):
        """The request body as a dict. An unparseable body is {} — every field
        is optional and defaults to the safe side, and a phone is as likely to
        POST nothing as to POST anything."""
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            return {}
        if length <= 0:
            return {}
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def _serve_static(self):
        if self.root is None or not self.root.is_dir():
            body = (
                b"devdash API is up, but the frontend has not been built.\n\n"
                b"  cd web && npm install && npm run build\n\n"
                b"API: /api/projects, /api/status, /healthz\n"
            )
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
    # SimpleHTTPRequestHandler serves relative to its `directory`, NOT the cwd
    # and not Handler.root — without this it happily lists the whole repository
    # (and, under launchd, whatever directory launchd started it in). The class
    # is built by the server as RequestHandlerClass(request, address, server),
    # so the directory is bound with a partial.
    handler = partial(Handler, directory=str(root)) if root is not None else Handler
    # Threading matters: /api/projects shells out to docker per project, and a
    # single-threaded server would freeze the phone on the first poll.
    httpd = ThreadingHTTPServer((host, port), handler)
    httpd.daemon_threads = True
    return httpd
