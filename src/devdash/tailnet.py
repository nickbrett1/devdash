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
_PROBE = (
    "if ! command -v tailscale >/dev/null 2>&1; then echo 'STATE=absent'; exit 0; fi; "
    "status=$(tailscale status 2>&1); "
    "case \"$status\" in "
    "  *'Logged out'*) echo 'STATE=logged_out';; "
    "  '') echo 'STATE=logged_out';; "
    "  *) echo 'STATE=connected';; "
    "esac; "
    "echo \"IP=$(tailscale ip -4 2>/dev/null | head -n1)\""
)

# Where the detached `tailscale up` writes its login URL, inside the container.
_LOG_PATH = "/tmp/devdash-tailscale.log"

_URL_RE = re.compile(r"https://login\.tailscale\.com/\S+")


def _docker(args, timeout=15):
    return subprocess.run([containers.DOCKER] + args, capture_output=True,
                          text=True, timeout=timeout, check=False)


def facts(container_id):
    """{"state": connected|logged_out|absent, "ip": str|None} for a container.

    Read-only: this never starts the daemon or registers anything, so it is
    safe on the project-list poll.
    """
    try:
        r = _docker(["exec", "-u", "root", container_id, "sh", "-c", _PROBE])
    except (OSError, subprocess.SubprocessError):
        return {"state": "absent", "ip": None}

    state, ip = "absent", None
    for line in (r.stdout or "").splitlines():
        if line.startswith("STATE="):
            state = line.split("=", 1)[1].strip() or "absent"
        elif line.startswith("IP="):
            ip = line.split("=", 1)[1].strip() or None
    return {"state": state, "ip": ip}


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


def _read_log(container_id):
    try:
        r = _docker(["exec", "-u", "root", container_id, "sh", "-c",
                     f"cat {_LOG_PATH} 2>/dev/null"], timeout=10)
    except (OSError, subprocess.SubprocessError):
        return ""
    return r.stdout or ""


def register(container_id, host, wait=8.0):
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

    # A previous detached `up` may still be waiting for its login URL; a second
    # one refuses to run ("another tailscale up is running"), so clear it first.
    # `-f 'tailscale up'` cannot match tailscaled itself.
    script = (
        "pkill -f 'tailscale up' >/dev/null 2>&1; "
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
        match = _URL_RE.search(out)
        if match:
            return {"started": True, "url": match.group(0), "detail": "authenticate to finish"}
        time.sleep(0.5)
    return {"started": True, "url": None,
            "detail": out.strip() or "tailscale up started; no login URL yet"}
