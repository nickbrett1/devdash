"""Configuration for devdash.

Lives at ~/.devdash/config.json (mode 600):

    {
      "host":  "",                  # bind address; "" = auto: tailnet IP, else 127.0.0.1
      "port":  3990,
      "web_dir": "web/dist"
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

CONFIG_DIR_NAME = ".devdash"
CONFIG_FILE_NAME = "config.json"
DEFAULT_PORT = 3990


def config_dir(home=None):
    return os.path.join(home or os.path.expanduser("~"), CONFIG_DIR_NAME)


def config_path(home=None):
    return os.path.join(config_dir(home), CONFIG_FILE_NAME)


def _defaults():
    return {"host": "", "port": DEFAULT_PORT, "web_dir": "web/dist"}


def save(cfg, home=None):
    """Atomically write config with 0600 permissions.

    0600 is now habit rather than necessity — there is no secret here — but a
    config that might later grow one should not have to remember to tighten."""
    path = config_path(home)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(cfg, f, indent=2)
        f.write("\n")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def load(home=None):
    """Load config, creating a default one if missing."""
    cfg = _defaults()
    path = config_path(home)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                cfg.update(data)
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Could not read {path}: {e}") from e
    # A "token" left in an older config is ignored, not honoured: the server
    # does not read it any more. Dropped so the file stops implying otherwise.
    cfg.pop("token", None)
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
