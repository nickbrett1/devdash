"""Where a running container sits on the tailnet — and the Blink host for it.

Open a project from a phone and the thing you actually want next is to *reach*
it: `ssh`/`mosh` over the tailnet, or a Blink Shell host that connects for you.
devopen registers the container (when an authkey is configured) and prints a
`blink://` link, but two cases leave the phone with nothing:

  * a container that was never registered, because there is no authkey and
    `tailscale up` needs a browser — which a server cannot offer to drive;
  * a container registered on an earlier run, whose link devopen prints but
    which the job log is the only place to read.

Host and user are facts about the container, not about who ran `open`, so
devdash reads the container's own tailscale state and rebuilds the same link
`devopen` would. Registration, when it is needed, is started *detached* here so
the login URL can be handed to the phone without a request that would otherwise
hang until someone authenticated.

Every function degrades to "absent" rather than raising: tailscale is optional
in a devcontainer, and a project list that 500s because one container has none
would be worse than one that says so.
"""

import os
import re
import shlex
import subprocess
import time

from devopen import opener  # blink_url lives there; one definition of the link
from devreap import containers

# One `docker exec` answers both questions. `tailscale status` needs the daemon
# socket, which is root-owned, so this runs as root — the same as devopen's own
# probe.
#
# Read the *JSON* backend state, not the human text. The text is not a contract
# and, worse, the old heuristic ("anything that is not 'Logged out' is
# connected") counted the daemon-down message — "failed to connect to local
# tailscaled" — as a healthy node. That is precisely the state of a container
# whose tailscaled has not started yet, so a fresh container was reported
# connected with no IP, the open job considered the tailnet step done, and
# nobody was offered a Register button. `--json` is emitted by the daemon
# itself (BackendState), so an unreachable daemon produces no BackendState at
# all instead of plausible-looking prose.
_PROBE = (
    "if ! command -v tailscale >/dev/null 2>&1; then echo 'STATE=absent'; exit 0; fi; "
    "json=$(tailscale status --json 2>/dev/null); "
    "echo \"BACKEND=$(printf '%s\\n' \"$json\" | "
    "sed -n 's/.*\"BackendState\"[[:space:]]*:[[:space:]]*\"\\([A-Za-z]*\\)\".*/\\1/p' | "
    "head -n1)\"; "
    "echo \"IP=$(tailscale ip -4 2>/dev/null | head -n1)\""
)

# BackendState values that mean "there is a node here, but it is not on the
# tailnet and `tailscale up` is the remedy". NeedsMachineAuth is the admin
# approval queue, which Register cannot clear, but it is still not connected.
_LOGGED_OUT = ("NeedsLogin", "NeedsMachineAuth", "Stopped")

# Where the detached `tailscale up` writes its login URL, inside the container.
_LOG_PATH = "/tmp/devdash-tailscale.log"

_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")


def _docker(args, timeout=15):
    return subprocess.run([containers.DOCKER] + args, capture_output=True,
                          text=True, timeout=timeout, check=False)


def _state(backend, ip):
    """BackendState (+ whether it has an address) as devdash's three states.

    `connected` requires an address: a node can report Running for a moment
    before it is assigned one, and an ssh target built from a hostname the
    tailnet cannot resolve is worse than an honest "not yet". Everything that
    is neither Running nor a login prompt — Starting, NoState, a daemon that
    did not answer at all (empty backend) — is `absent`: there is nothing to
    authenticate *yet*.
    """
    if backend == "Running":
        return "connected" if ip else "absent"
    if backend in _LOGGED_OUT:
        return "logged_out"
    return "absent"


def _parse(output):
    """(state, ip) from the probe's KEY=value lines."""
    backend, ip = "", None
    for line in (output or "").splitlines():
        if line.startswith("STATE="):
            return line.split("=", 1)[1].strip() or "absent", ip
        if line.startswith("BACKEND="):
            backend = line.split("=", 1)[1].strip()
        elif line.startswith("IP="):
            ip = line.split("=", 1)[1].strip() or None
    return _state(backend, ip), ip


def facts(container_id):
    """{"state": connected|logged_out|absent, "ip": str|None} for a container.

    Read-only: this never starts the daemon or registers anything, so it is
    safe on the project-list poll.
    """
    try:
        r = _docker(["exec", "-u", "root", container_id, "sh", "-c", _PROBE])
    except (OSError, subprocess.SubprocessError):
        return {"state": "absent", "ip": None}

    state, ip = _parse(r.stdout)
    return {"state": state, "ip": ip}


def present(container_id):
    """True if the tailscale CLI exists in the container at all.

    `facts` reports `absent` both for "no tailscale here" and "the daemon has
    not answered yet". A caller that would *wait* for the daemon, or start it,
    has to know which one it is looking at.
    """
    try:
        r = _docker(["exec", "-u", "root", container_id, "sh", "-c",
                     "command -v tailscale >/dev/null 2>&1"])
    except (OSError, subprocess.SubprocessError):
        return False
    return r.returncode == 0


def wait_for_tailscale(container_id, timeout=15.0, interval=1.5):
    """Poll until the daemon answers, or `timeout` elapses; return the state.

    A container has the tailscale binary for a while before its tailscaled is
    up — `devcontainer up` returns first, and postStart starts the daemon
    behind it. Reading `absent` once and calling it final is what let an open
    finish as if the tailnet step did not exist, so the gate waits here
    instead of guessing. Only waits when there is something to wait for: the
    caller checks `present` first.
    """
    deadline = time.monotonic() + timeout
    while True:
        state = facts(container_id)["state"]
        if state != "absent" or time.monotonic() >= deadline:
            return state
        time.sleep(interval)


# Bring tailscaled up the same way devopen does: the container may have the
# binary and the persisted state (so the node reconnects without a login) but
# no daemon, because a plain `docker start` does not run postStart.
_START_DAEMON = (
    "command -v tailscaled >/dev/null 2>&1 || exit 0; "
    "pgrep -x tailscaled >/dev/null 2>&1 && exit 0; "
    "start-stop-daemon --start --background --oknodo "
    "--pidfile /var/run/tailscaled.pid --make-pidfile "
    "--exec /usr/sbin/tailscaled -- --state=/var/lib/tailscale/tailscaled.state"
)


def _remote_user(workspace):
    """remoteUser from the workspace's devcontainer config (default 'vscode').

    Mirrors devopen's own reader rather than importing the private helper: the
    config is the container's, and this is the same file devopen reads.
    """
    import json
    for path in (os.path.join(workspace, ".devcontainer", "devcontainer.json"),
                 os.path.join(workspace, ".devcontainer.json")):
        if os.path.isfile(path):
            try:
                with open(path, encoding="utf-8") as f:
                    return json.load(f).get("remoteUser") or "vscode"
            except (OSError, ValueError):
                break
    return "vscode"


def _blink_key():
    """The Blink key name from devopen's config, if any — the same key devopen
    would put in the link, so the two never disagree."""
    try:
        from devopen import config as devopen_config
        return (devopen_config.load().get("blink_key") or "").strip() or None
    except Exception:  # noqa: BLE001 — a missing config must not break the list
        return None


def connection(container_id, workspace, host=None):
    """Everything the phone needs to reach this container.

    `host` is the MagicDNS name the container registers under; it is the
    workspace basename by construction (devopen registers with
    `repo_dir_name(repo_url)`, and that is the directory it clones into).
    """
    host = host or os.path.basename(workspace.rstrip("/"))
    found = facts(container_id)
    user = _remote_user(workspace)
    state = found["state"]
    ip = found["ip"]
    blink = None
    if state == "connected":
        try:
            blink = opener.blink_url(host, user, key=_blink_key())
        except Exception:  # noqa: BLE001 — a bad key must not hide the state
            blink = opener.blink_url(host, user)
    return {
        "state": state,
        "host": host,
        "user": user,
        "ip": ip,
        # Prefer the stable MagicDNS name for ssh — it survives a re-register
        # where the IP does not, and it is the same name the Blink host uses.
        # The IP is the fallback for a node whose name will not resolve yet.
        "ssh": (f"ssh {user}@{host}" if state == "connected"
                else (f"ssh {user}@{ip}" if ip else None)),
        "blink": blink,
    }


def wait_connected(container_id, timeout, interval=3.0, on_wait=None):
    """Poll until the container reports `connected`, or `timeout` elapses.

    This is the half a server cannot do for itself: `tailscale up` prints a
    login URL and blocks until a browser finishes the flow. devdash starts it
    detached and hands the URL to the phone, then waits here — the open job
    stands still, exactly as the CLI stands still, until someone authenticates.
    Returns True once connected, False on timeout.
    """
    deadline = time.monotonic() + timeout
    while True:
        state = facts(container_id)["state"]
        if state == "connected":
            return True
        # The gate only waits after reading `logged_out`, so `absent` now means
        # the container (or its daemon) went away: there is nothing left to
        # authenticate, and holding the job open for the full timeout would just
        # block a retry.
        if state == "absent":
            return False
        if time.monotonic() >= deadline:
            return False
        if on_wait:
            on_wait()
        time.sleep(interval)


def _read_log(container_id):
    try:
        r = _docker(["exec", "-u", "root", container_id, "sh", "-c",
                     f"cat {_LOG_PATH} 2>/dev/null"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout or ""


def _read_login_url(container_id, log=""):
    """The login URL, from the detached run's log or, failing that, `status`.

    The log is written by our own `tailscale up`, so it is the direct source.
    But `tailscale status` prints the same URL for a logged-out node, so it is a
    fallback for the case where the detached run's output went somewhere
    unexpected — better a URL from the daemon than "no login URL yet" against a
    node that plainly has one.
    """
    match = _URL_RE.search(log)
    if match:
        return match.group(0)
    try:
        r = _docker(["exec", "-u", "root", container_id, "tailscale", "status"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    match = _URL_RE.search(r.stdout or "")
    return match.group(0) if match else None


def register(container_id, host, wait=8.0, daemon_wait=10.0):
    """Start `tailscale up` detached; return the login URL once it appears.

    `tailscale up` without an authkey prints a login URL and then *blocks* until
    a browser finishes the flow — the one thing a server cannot do. Run it
    detached inside the container instead: the URL lands in a file, the process
    keeps waiting, and the phone opens the URL. Authentication then completes on
    its own, and the next probe reads the container as connected.

    Returns {"started": bool, "url": str|None, "detail": str}.
    """
    if facts(container_id)["state"] == "connected":
        return {"started": False, "url": None,
                "detail": f"{host} is already registered on the tailnet"}

    # No login URL to hand out is not the only way this can be fixed: a node
    # whose state was persisted reconnects by itself as soon as its daemon
    # runs, and needs no browser at all. That is the ordinary case after a
    # container restart, and `tailscale up` against a dead daemon would only
    # fail, so start the daemon first and give it a moment to answer.
    if present(container_id):
        try:
            _docker(["exec", "-u", "root", container_id, "sh", "-c", _START_DAEMON],
                    timeout=20)
        except (OSError, subprocess.SubprocessError):
            pass  # the `up` below reports the real failure if the daemon is gone
        if wait_for_tailscale(container_id, timeout=daemon_wait) == "connected":
            return {"started": False, "url": None,
                    "detail": f"{host} reconnected to the tailnet"}

    # A previous detached `up` may still be waiting for its login URL; a second
    # one refuses to run ("another tailscale up is running"), so clear it first.
    # Match by process *name* (`-x tailscale`), never by command line: this
    # script runs under `sh -c "<script>"`, so the shell's own argv contains the
    # literal text "tailscale up". `pkill -f 'tailscale up'` therefore matches
    # and SIGTERMs the very shell running it, the `rm`/`nohup` never execute, no
    # log file is written, and `register` always came back "no login URL yet" —
    # even against a node whose `tailscale status` printed a URL the whole time.
    # `-x tailscale` matches the CLI's comm name and is safe: tailscaled is a
    # different name and the shell is "sh".
    script = (
        "pkill -x tailscale >/dev/null 2>&1; "
        f"rm -f {_LOG_PATH}; "
        f"nohup tailscale up --hostname={shlex.quote(host)} > {_LOG_PATH} 2>&1 &"
    )
    try:
        _docker(["exec", "-d", "-u", "root", container_id, "sh", "-c", script], timeout=20)
    except (OSError, subprocess.SubprocessError) as e:
        return {"started": False, "url": None, "detail": f"could not start tailscale up: {e}"}

    deadline = time.monotonic() + wait
    out = ""
    while time.monotonic() < deadline:
        out = _read_log(container_id)
        url = _read_login_url(container_id, out)
        if url:
            return {"started": True, "url": url, "detail": "authenticate to finish"}
        time.sleep(0.5)
    return {"started": True, "url": None,
            "detail": out.strip() or "tailscale up started; no login URL yet"}
