"""Configuration for devdash.

Lives at ~/.devdash/config.json (mode 600):

    {
      "host":  "",                  # bind address; "" = auto: tailnet IP, else 127.0.0.1
      "port":  3990,
      "web_dir": "web/dist",
      "mcp":   {"enabled": false, "tools": []}   # HTTP MCP endpoint; off by default
    }

Thresholds are deliberately NOT duplicated here. The Buildkite token and org,
the pipeline map, and the keep/ignore lists all come from **devreap's** config
(~/.devreap/config.json), so devdash and the nightly reap can never disagree
about what "idle" means. devdash adds only what is about *being a server*.

**There is no token.** Access control is the *bind*: devdash listens on the
tailnet address and nothing else (see bind_host), so the only thing that can
reach it is a device on the tailnet. A shared secret on top of that was one
more thing to paste into a phone for no real gain — see the README.
"""

import json
import os
import tempfile

CONFIG_DIR_NAME = ".devdash"
CONFIG_FILE_NAME = "config.json"
DEFAULT_PORT = 3990


def config_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), CONFIG_DIR_NAME)


def config_path(home=None):
    return os.path.join(config_dir(home), CONFIG_FILE_NAME)


def _defaults():
    return {"host": "", "port": DEFAULT_PORT, "web_dir": "web/dist",
            # The MCP endpoint is off until an operator turns it on: it is a
            # mutating surface an agent drives, and the tools are the same ones
            # that stop containers. `tools: []` means the default set (every
            # tool except provision_project); a non-empty list is an explicit
            # allowlist, so provision_project is reachable only by naming it.
            "mcp": {"enabled": False, "tools": []}}


def save(cfg, home=None):
    """Atomically write config with 0600 permissions.

    0600 is now habit rather than necessity — there is no secret here — but a
    config that might later grow one should not have to remember to tighten.

    The temp file is uniquely named on purpose. Several threads share this
    module — request handlers, job threads, the warm-up thread — and a fixed
    `path + ".tmp"` lets one thread's `os.replace` pull the file out from under
    another's `open`/`os.chmod`, which is how a provision turned into a
    spurious `FileNotFoundError: .../config.json.tmp`."""
    path = config_path(home)
    directory = os.path.dirname(path)
    os.makedirs(directory, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=os.path.basename(path) + ".",
                               suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
            f.write("\n")
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def load(home=None):
    """Load config, creating a default one if missing."""
    cfg = _defaults()
    path = config_path(home)
    existed = os.path.exists(path)
    stale_token = False
    if existed:
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                stale_token = "token" in data
                cfg.update(data)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Could not read {path}: {e}") from e
    # A "token" left in an older config is ignored, not honoured: the server
    # does not read it any more. Dropped so the file stops implying otherwise.
    cfg.pop("token", None)
    # Write only when there is a reason to: first run, or an old "token" to
    # drop. A plain read is no longer a write, so threads sharing this module
    # cannot collide in save() over a config they only came to read.
    if not existed or stale_token:
        save(cfg, home=home)
    return cfg


def tailnet_ip():
    """The host's tailnet address, or None. Best-effort — this Mac is the
    tailnet member, so the binary is normally present."""
    import subprocess
    for exe in ("/usr/local/bin/tailscale", "/opt/homebrew/bin/tailscale", "tailscale"):
        try:
            r = subprocess.run([exe, "ip", "-4"], capture_output=True, text=True,
                               timeout=10, check=False)
        except (OSError, subprocess.SubprocessError):
            continue
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout.strip().splitlines()[0]
    return None


def bind_host(cfg):
    """Where to listen. Never the default-open 0.0.0.0 unless asked for it
    explicitly: devdash can stop containers, so it does not want to be on
    every network this Mac can see."""
    host = (cfg.get("host") or "").strip()
    if host:
        return host
    return tailnet_ip() or "127.0.0.1"
