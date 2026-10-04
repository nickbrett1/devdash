"""Open and close a project. The mutating half of devdash.

Two differences from the CLIs this reuses, both because devdash is a server
with no terminal:

  * **There is no tty, so there are no prompts.** devopen asks about rebasing,
    removing untracked files, and registering tailscale; devreap refuses to
    stop a container someone is sitting in. Every one of those becomes an
    explicit request parameter with a safe default, never a question we cannot
    ask.
  * **The live-session veto is honoured, and overridable.** A phone must be
    able to say "yes, I know" — that is `force` — but the default is to refuse
    and say why, because a mis-tap that kills a live session is silent while a
    refusal is not.
"""

import os
import shlex
import subprocess

from devopen import config as devopen_config
from devopen import opener
from devreap import config as reap_config
from devreap import containers, vscode

from . import tailnet

# Mirror devopen's one-shot, non-destructive path. `fresh` would delete an
# existing container and rebuild it (a prompt on the CLI), `clean` would blow
# away untracked files (another prompt); both stay off unless a caller asks.
SAFE = {"fresh": False, "clean": False}


class ActionError(RuntimeError):
    """A request devdash will not carry out — the caller's fault, so a 4xx."""


def _row(name):
    """The project row for `name`, or raise. Reuses the same join the list
    uses, so an action can never act on a project the UI did not show — and a
    name typed by hand resolves through exactly the same rules."""
    from . import projects

    rows, _ = projects.rows(probe_builds=False)
    for row in rows:
        if row["name"] == name:
            return row
    raise ActionError(f"unknown project '{name}'")


def _log_collector(limit=40):
    """An on_log sink that keeps the tail of the output. devopen's functions
    log a line at a time; a server has nowhere to print them, so they become
    the response's `log` field instead of the LaunchAgent's stderr."""
    lines = []

    def on_log(line):
        lines.append(str(line))
        del lines[:-limit]

    return lines, on_log


def close_project(name, force=False):
    """Stop the container and close its VS Code window.

    Best-effort on the window and precise about it: a "save your changes?"
    sheet can keep a window open, and the container is stopped either way.
    Reporting which half succeeded is the honest answer; failing the whole
    request would leave the caller unsure whether RAM was reclaimed.
    """
    row = _row(name)
    if row["state"] == "absent":
        return {"name": name, "stopped": False, "window_closed": False,
                "detail": "no container to stop"}

    if row["live_session"] and not force:
        return {"name": name, "stopped": False, "window_closed": False,
                "refused": True,
                "detail": f"live session ({row['live_evidence']}); "
                          f"pass force to stop anyway"}

    container = row["container"]
    stop_detail = ""
    if row["state"] == "running":
        stopped, stop_detail = containers.stop(container)
    else:
        # Already stopped. Not a failure, and there is nothing to reclaim — but
        # the window is still worth closing, which is the whole point of
        # treating the two halves separately.
        stopped = True

    # Always attempt the window close, even when `window_open` is false. System
    # Events reports no windows while the screen is locked, so a false here
    # does not mean there is nothing to close — and close_workspace_window
    # answers "no open window for this workspace" cheaply when there truly
    # is none.
    closed, window_detail = vscode.close_workspace_window(row["path"])

    # The window is the half that can half-fail, so its detail is the default
    # answer. The stop's own output only earns a mention when the stop is what
    # went wrong — otherwise every successful close would read like a log line.
    detail = window_detail
    if not stopped and stop_detail:
        detail = f"container: {stop_detail}"
        if window_detail:
            detail += f" | window: {window_detail}"
    return {"name": name, "stopped": stopped, "window_closed": closed,
            "container": container, "detail": detail}


def open_project(name, fresh=None, clean=None, on_log=None, on_attention=None):
    """Clone if needed, `devcontainer up`, and open the window.

    devopen's open_repo already does all of this — including cloning when the
    workspace is missing, which is why M3's provision flow is mostly a matter
    of pointing at a repo that has no workspace directory yet.
    """
    row = _row(name)
    # `on_log` is how a job streams this to a phone; without one the tail of
    # the output rides along in the response instead, which is what a direct
    # call (and every test) wants.
    lines, collect = _log_collector()
    sink = on_log or collect
    uri = _open(row["name"], _workspaces_dir(row), sink,
                fresh=fresh, clean=clean)
    result = {"name": name, "uri": uri, "log": lines}
    tail = tailscale_gate(name, sink, on_attention)
    if tail is not None:
        result["tailscale"] = tail
    return result


def provision(repo, on_log, fresh=None, clean=None, on_attention=None):
    """Clone, build and open a repo that has no workspace here yet.

    The only real difference from `open_project` is the *input*: a repository
    (`owner/name` or a URL) rather than a project name, because the project
    does not exist yet — that is the whole point. `open_repo` clones when the
    directory is missing, so the flow is genuinely the same code path.
    """
    repo = (repo or "").strip()
    if not repo:
        raise ActionError("a repository is required")

    expanded = opener.normalize_repo(repo)
    if not expanded:
        raise ActionError(f"could not read '{repo}' as a repository")
    name = opener.repo_dir_name(expanded)

    cfg = reap_config.load()
    workspaces = cfg.get("workspaces_dir")
    uri = _open(expanded, workspaces, on_log, fresh=fresh, clean=clean)
    result = {"name": name, "repo": expanded, "uri": uri}
    tail = tailscale_gate(name, on_log, on_attention)
    if tail is not None:
        result["tailscale"] = tail
    return result


def register_tailscale(name, on_log=None, on_attention=None, wait=None):
    """Register a running container on the tailnet, and start its agent.

    The manual counterpart to the gate open/provision run. It is a job because
    it *waits*: `tailscale up` blocks until a browser finishes the login, and an
    HTTP request must not. The phone opens the URL from the job's `attention`
    and the job completes on its own, then re-runs the post-start hook so the
    container agent starts — including on a container that was already on the
    tailnet but whose agent had not come up.
    """
    row = _row(name)
    if row["state"] != "running":
        raise ActionError(f"{name} is not running — open it first")
    sink = on_log or (lambda line: None)
    result = tailscale_gate(name, sink, on_attention, wait=wait, force_agent=True)
    if result is None:
        state = tailnet.facts(row["container"])["state"]
        detail = ("no tailscale in this container" if state == "absent"
                  else "no container to register")
        return {"name": name, "state": state, "started": False, "url": None,
                "detail": detail}
    return {"name": name, "started": True, **result}


# How long an open/provision job will stand still waiting for someone to finish
# the Tailscale login. Long enough for a phone to open the URL and authenticate
# (the CLI waits forever; a server should not), short enough that an ignored
# prompt does not hold a job — and its 409 — open all afternoon.
TAILSCALE_WAIT = 600


def tailscale_gate(name, on_log, on_attention=None, wait=None, force_agent=False):
    """The step the CLI takes mid-open: join the tailnet before finishing.

    devopen prompts on a tty and runs `tailscale up` in the foreground, so the
    window opens *after* the container has an address and the post-start hook
    can start its agent. A server cannot prompt, and answering `tailscale=False`
    (what devdash used to do without an authkey) skipped the step entirely —
    the container came up logged out, `agent-dev.sh` could not resolve an
    address, and the agent never started.

    So do it here: register detached, hand the URL to the phone, *wait* for the
    login to complete, then re-run the post-start hook so the agent starts now
    that an address exists.

    Returns None when there is nothing to do (no container, no tailscale, or
    already connected) so an open/provision can tell "skipped" from "did
    something". `force_agent` makes an already-connected container re-run its
    post-start hook anyway — the manual retry wants that, because a container
    registered on an earlier run (or whose agent failed this run) is exactly the
    case it is there to fix; `postStartCommand` is idempotent, so it is safe.
    """
    container = _container_for(name)
    if not container or not container.get("running"):
        return None
    cid = container["id"]
    state = tailnet.facts(cid)["state"]
    if state == "absent":
        return None
    if state == "connected":
        if not force_agent:
            return None
        started, detail = start_agent(cid, container["workspace"])
        on_log(f"[devdash] agent: {detail}")
        return {"state": "connected", "url": None, "connected": True,
                "agent_started": started, "detail": detail}

    result = tailnet.register(cid, name)
    url = result.get("url")
    on_log(f"[devdash] tailscale: {result['detail']}")
    if not url:
        return {"state": "logged_out", **result}

    on_log(f"[devdash] tailscale: open {url} to authenticate; this job waits, then starts the agent.")
    if on_attention:
        on_attention({
            "kind": "tailscale",
            "url": url,
            "host": name,
            "detail": f"Authenticate {name} on the tailnet to finish",
        })

    if not tailnet.wait_connected(cid, TAILSCALE_WAIT if wait is None else wait):
        on_log("[devdash] tailscale: still logged out — the container agent will not start "
               "until the container is on the tailnet.")
        return {"state": "logged_out", "url": url, "connected": False,
                "detail": "still waiting for the tailnet login"}

    on_log("[devdash] tailscale: authenticated.")
    started, detail = start_agent(cid, container["workspace"])
    on_log(f"[devdash] agent: {detail}")
    return {"state": "connected", "url": url, "connected": True,
            "agent_started": started, "detail": detail}


def start_agent(container_id, workspace):
    """Re-run the container's post-start hook so its agent starts.

    The agent is started by `postStartCommand`, which `devcontainer up` already
    ran once — too early, before the container had a tailnet address, so
    `agent-dev.sh` refused to start it. Re-running the hook now is what the CLI
    gets for free by registering before it opens the window (the extension
    re-runs postStart on attach). Best-effort: a project is usable without an
    agent, so a failure is a detail, not an ActionError.

    It runs as the container's *own* user with the right HOME, not the
    `remoteUser` default — this repo's user is `node`, and `docker exec -u
    vscode` would fail while `-u node` without HOME would write the agent's
    config to the wrong place.
    """
    config = _devcontainer_config(workspace)
    command = _post_start_command(config)
    if not command:
        return False, "no postStartCommand to re-run; start the agent by hand if needed"
    user, home = _container_user(container_id, config, workspace)
    argv = [containers.DOCKER, "exec"]
    if user:
        argv += ["-u", user]
    if home:
        argv += ["-e", f"HOME={home}"]
    workdir = config.get("workspaceFolder")
    if isinstance(workdir, str) and workdir:
        argv += ["-w", workdir]
    argv += [container_id] + command
    try:
        r = subprocess.run(argv, capture_output=True, text=True, timeout=300, check=False)
    except (OSError, subprocess.SubprocessError) as e:
        return False, f"could not re-run the post-start hook: {e}"
    if r.returncode != 0:
        last = ((r.stderr or r.stdout or "").strip().splitlines() or [""])[-1]
        return False, f"the post-start hook exited {r.returncode}" + (f": {last}" if last else "")
    return True, "re-ran the post-start hook so the container agent starts"


def _devcontainer_config(workspace):
    """The workspace's devcontainer config as a dict, or {}. Same two paths the
    devcontainer CLI reads."""
    import json

    for path in (os.path.join(workspace, ".devcontainer", "devcontainer.json"),
                 os.path.join(workspace, ".devcontainer.json")):
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    config = json.load(f)
            except (OSError, ValueError):
                return {}
            return config if isinstance(config, dict) else {}
    return {}


def _post_start_command(config):
    """`postStartCommand` from a devcontainer config, as argv.

    The string form (what the generator writes) runs through a shell; the array
    form is already argv. Anything else — an absent key, an object — is no
    command, because guessing one would run the wrong thing in someone's
    container.
    """
    command = config.get("postStartCommand")
    if isinstance(command, str) and command.strip():
        return ["sh", "-lc", command]
    if isinstance(command, list) and command and all(isinstance(a, str) for a in command):
        return command
    return None


def _container_user(container_id, config, workspace):
    """(user, home) for the container's working user, or (None, None) to let
    docker pick. `remoteUser` wins; otherwise the owner of the workspace folder
    is the user the devcontainer runs as. Read as root — the workspace may be
    owned by a user this process is not."""
    name = config.get("remoteUser")
    if not (isinstance(name, str) and name):
        workdir = config.get("workspaceFolder") or ("/workspaces/" + os.path.basename(workspace.rstrip("/")))
        out = _exec_root(container_id, f"stat -c %U {shlex.quote(workdir)} 2>/dev/null")
        name = (out or "").strip() or None
    if not name:
        return None, None
    home = (_exec_root(container_id, f"getent passwd {shlex.quote(name)} 2>/dev/null | cut -d: -f6") or "").strip()
    if not home and name != "root":
        home = f"/home/{name}"
    return name, home or None


def _exec_root(container_id, script, timeout=30):
    """Run a shell string in the container as root; '' on any failure."""
    try:
        r = subprocess.run(
            [containers.DOCKER, "exec", "-u", "root", container_id, "sh", "-c", script],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout or ""


def _container_for(name):
    """The devcontainer whose workspace is `<name>`, or None. Reads docker
    directly rather than the cached list the UI sampled: an open or provision
    has just changed that list, and this needs the container it made."""
    try:
        for c in containers.list_devcontainers():
            if os.path.basename((c.get("workspace") or "").rstrip("/")) == name:
                return c
    except Exception:  # noqa: BLE001 — the gate is best-effort; a docker fault must not fail an open
        return None
    return None


def _open(repo_ref, workspaces_dir, on_log, fresh=None, clean=None):
    """The one call into devopen, shared by open and provision."""
    opts = dict(SAFE)
    if fresh is not None:
        opts["fresh"] = bool(fresh)
    if clean is not None:
        opts["clean"] = bool(clean)
    try:
        return opener.open_repo(
            repo_ref,
            workspaces_dir=workspaces_dir,
            tailscale=_should_register_tailscale(),
            on_log=on_log,
            **opts,
        )
    except opener.DevopenError as e:
        raise ActionError(str(e)) from e


def _should_register_tailscale():
    """Whether `open` should also put the container on the tailnet.

    devopen treats `None` as "ask", which a server cannot do, and `False` as
    "never" — but never was wrong. Registering is not a side effect here, it is
    most of the point: the reason to open a project from a phone is to reach it
    from the phone, which means ssh, a terminal app, or VS Code, all of which
    need a MagicDNS name.

    `tailscale up` without an authkey prints a login URL that needs a browser,
    which is no use from a server — so this is True only when devopen's config
    has a key to do it non-interactively. No key means silence, not a hang.
    """
    return bool((devopen_config.load().get("tailscale_authkey") or "").strip())


def _workspaces_dir(row):
    """The directory devopen should work in. devreap's config is the list's
    source of truth for workspaces_dir (every row came from it), so passing
    anything else could clone beside the directory the UI is showing. The row's
    own path is the last resort: it is `<workspaces_dir>/<name>` by
    construction."""
    cfg = reap_config.load()
    return cfg.get("workspaces_dir") or os.path.dirname(row["path"]) or None
