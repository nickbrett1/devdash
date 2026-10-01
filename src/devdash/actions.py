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

from devopen import config as devopen_config
from devopen import opener
from devreap import config as reap_config
from devreap import containers, vscode

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


def open_project(name, fresh=None, clean=None, on_log=None):
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
    uri = _open(row["name"], _workspaces_dir(row), on_log or collect,
                fresh=fresh, clean=clean)
    return {"name": name, "uri": uri, "log": lines}


def provision(repo, on_log, fresh=None, clean=None):
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
    return {"name": name, "repo": expanded, "uri": uri}


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
    from the phone, and the Blink link devdash shows is only real once the
    container has a MagicDNS name.

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
