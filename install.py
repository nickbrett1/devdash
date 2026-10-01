#!/usr/bin/env python3
"""Install devdash: the venv, the built frontend, and the always-on agent.

devdash is a long-running web server on the Mac (never a container — it has to
reach docker and System Events on the host), so unlike devreap's nightly job
this installs a LaunchAgent with KeepAlive.

Run it standalone (no clone needed):
    curl -fsSL https://raw.githubusercontent.com/nickbrett1/devdash/main/install.py | python3

Overrides (env):
    DEVOPEN_HOME          default ~/DevOpen
    DEVOPEN_WORKSPACES    default $DEVOPEN_HOME/workspaces  (devdash lives here)
    DEVDASH_HOME          default $DEVOPEN_WORKSPACES/devdash
    DEVDASH_PYTHON        interpreter to build the venv on
                          (default /opt/homebrew/bin/python3.14 — see below)
    DEVDASH_SKIP_UPDATE=1 skip git pull of an existing checkout
    DEVDASH_SKIP_WEB=1    skip the frontend build
    DEVDASH_SKIP_AGENT=1  don't install/load the LaunchAgent
"""

import json
import os
import shutil
import subprocess

REPO_URL = "https://github.com/nickbrett1/devdash.git"
LABEL = "com.nickbrett1.devdash"
LOG_NAME = "devdash.log"

# Closing a VS Code window goes through System Events, and macOS grants
# Accessibility to *the interpreter* — realpath(sys.executable). The Homebrew
# 3.14 binary is the one already granted; a venv built on it resolves through
# to the same binary, so the grant carries over. Any other interpreter gets a
# fresh prompt for one you did not mean to bless, and the window close then
# fails silently under launchd.
PREFERRED_PYTHON = "/opt/homebrew/bin/python3.14"
PYTHON_FALLBACKS = (
    "/opt/homebrew/opt/python@3.14/bin/python3.14",
    "/usr/local/bin/python3.14",
)
# launchd hands an agent the bare PATH /usr/bin:/bin:/usr/sbin:/sbin, which has
# neither /usr/local/bin (docker) nor /opt/homebrew/bin (python3, node).
AGENT_PATH = "/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"


def sh(cmd, check=True, cwd=None, **kw):
    print("$ " + " ".join(str(c) for c in cmd), flush=True)
    return subprocess.run(cmd, check=check, cwd=cwd, **kw)


def out(cmd, cwd=None):
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=cwd, check=False)
    if r.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)}: {(r.stderr or r.stdout).strip()}")
    return r.stdout.strip()


def find_python():
    """The interpreter the venv must be built on, and whether it is the right one."""
    explicit = os.environ.get("DEVDASH_PYTHON")
    if explicit:
        return explicit, os.path.realpath(explicit).endswith("python3.14")
    for candidate in (PREFERRED_PYTHON, *PYTHON_FALLBACKS):
        if os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate, True
    found = shutil.which("python3.14") or shutil.which("python3")
    if not found:
        raise SystemExit("No python3 found. Install one, or set DEVDASH_PYTHON.")
    return found, os.path.realpath(found).endswith("python3.14")


def npm_command(web_dir):
    """The npm to build with — the one package.json pins, run via npx.

    package.json declares `packageManager: npm@11.19.1`, and the reason is not
    tidiness: the system npm 10.9.2 crashes resolving this dependency tree
    ("Cannot read properties of null (reading 'edgesOut')" inside arborist).
    The CI step installs the pinned npm for the same reason; do it here too, so
    an install on a fresh Mac does not fail at the last step.
    """
    pinned = ""
    try:
        with open(os.path.join(web_dir, "package.json"), encoding="utf-8") as f:
            pinned = json.load(f).get("packageManager") or ""
    except (OSError, ValueError):
        pass
    return ["npx", "--yes", pinned] if pinned.startswith("npm@") else ["npm"]


def write_agent(install_dir, config_dir, python):
    src = os.path.join(install_dir, "launchd", f"{LABEL}.plist")
    if not os.path.isfile(src):
        print("⚠️  launchd template missing — skipping the agent.")
        return None
    os.makedirs(config_dir, exist_ok=True)
    log_path = os.path.join(config_dir, LOG_NAME)
    dest = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    with open(src, encoding="utf-8") as f:
        body = (
            f.read()
            .replace("__DEVDASH_PYTHON__", python)
            .replace("__DEVDASH_DIR__", install_dir)
            .replace("__DEVDASH_LOG__", log_path)
            .replace("__DEVDASH_PATH__", AGENT_PATH)
        )
    with open(dest, "w", encoding="utf-8") as f:
        f.write(body)
    print(f"LaunchAgent written: {dest}")
    return dest


def load_agent(dest):
    uid = os.getuid()
    # bootout ignores a not-yet-loaded job; bootstrap then loads it.
    subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                   capture_output=True, text=True, check=False)
    r = subprocess.run(["launchctl", "bootstrap", f"gui/{uid}", dest],
                       capture_output=True, text=True, check=False)
    if r.returncode == 0:
        print("LaunchAgent loaded — devdash starts now and stays up.")
        return True
    print("⚠️  Could not load the LaunchAgent automatically. Load it with:")
    print(f"    launchctl bootstrap gui/{uid} {dest}")
    return False


def main():
    home = os.path.expanduser("~")
    devopen_home = os.environ.get("DEVOPEN_HOME") or os.path.join(home, "DevOpen")
    workspaces_dir = os.environ.get("DEVOPEN_WORKSPACES") or os.path.join(devopen_home, "workspaces")
    install_dir = os.environ.get("DEVDASH_HOME") or os.path.join(workspaces_dir, "devdash")
    config_dir = os.path.join(home, ".devdash")
    venv = os.path.join(install_dir, ".venv")
    vpython = os.path.join(venv, "bin", "python")

    python, correct = find_python()
    print(f"Installing devdash (web server + always-on LaunchAgent)…\n"
          f"  install dir:   {install_dir}\n"
          f"  interpreter:   {python}\n"
          f"                 realpath {os.path.realpath(python)}")
    if not correct:
        print("⚠️  That is NOT the Accessibility-granted Homebrew python3.14. The\n"
              "    server will run, but closing VS Code windows (M2) will silently\n"
              "    fail, and macOS may prompt for a fresh Accessibility grant.\n"
              "    Re-run with DEVDASH_PYTHON=/opt/homebrew/bin/python3.14 when you can.")

    print("\n[1/7] Repository…")
    if not os.path.isdir(os.path.join(install_dir, ".git")):
        os.makedirs(os.path.dirname(install_dir), exist_ok=True)
        sh(["git", "clone", REPO_URL, install_dir])
    elif os.environ.get("DEVDASH_SKIP_UPDATE") != "1":
        sh(["git", "-C", install_dir, "pull", "--ff-only"], check=False)
    else:
        print("Skipping repo update (DEVDASH_SKIP_UPDATE=1).")

    print("\n[2/7] Virtualenv…")
    if not os.path.isfile(vpython):
        sh([python, "-m", "venv", venv])
    print(f"  {vpython}")
    print(f"  resolves to {os.path.realpath(vpython)}")

    print("\n[3/7] Installing devdash and its pinned dependencies…")
    sh([vpython, "-m", "pip", "install", "--quiet", "--upgrade", "pip"])
    # Editable: the repo *is* the install, so `git pull` + restart is the update.
    # The devopen/devreap git dependencies pinned to tags in pyproject.toml are
    # fetched here. Re-pinning one later needs --force-reinstall (pip considers
    # an already-satisfied name satisfied and keeps the old commit).
    sh([vpython, "-m", "pip", "install", "--quiet", "-e", install_dir])

    print("\n[4/7] Writing config and token…")
    os.makedirs(config_dir, exist_ok=True)
    # Let devdash generate the config, so the token in the URL below is exactly
    # the one the server will check.
    token = out([vpython, "-c", "from devdash import config; print(config.load()['token'])"])
    host = out([vpython, "-c", "from devdash import config; print(config.bind_host(config.load()))"])
    port = out([vpython, "-c", "from devdash import config; print(config.load()['port'])"])
    print(f"  {os.path.join(config_dir, 'config.json')} (mode 600)")

    print("\n[5/7] Frontend…")
    if os.environ.get("DEVDASH_SKIP_WEB") == "1":
        print("Skipped (DEVDASH_SKIP_WEB=1).")
    elif shutil.which("npm") is None:
        print("⚠️  npm not found — the frontend will not be built. The API still works.")
    else:
        web = os.path.join(install_dir, "web")
        lock = os.path.join(web, "package-lock.json")
        npm = npm_command(web)
        sh(npm + ["ci" if os.path.isfile(lock) else "install"], cwd=web)
        sh(npm + ["run", "build"], cwd=web)
        print(f"  built → {os.path.join(web, 'dist')}")

    print("\n[6/7] LaunchAgent…")
    if os.environ.get("DEVDASH_SKIP_AGENT") == "1":
        print("Skipped (DEVDASH_SKIP_AGENT=1).")
    else:
        dest = write_agent(install_dir, config_dir, vpython)
        if dest:
            load_agent(dest)

    print()
    print("=" * 62)
    print("  devdash installed 📊")
    print("=" * 62)
    print()
    print(f"  http://{host}:{port}/?token={token}")
    print()
    print("Open that once (from the phone too) — it trades the token for an")
    print("HttpOnly cookie and drops it from the URL. Bookmark the clean URL after.")
    print(f"\n  token:  {token}")
    print(f"  logs:   {os.path.join(config_dir, LOG_NAME)}")
    print(f"  config: {os.path.join(config_dir, 'config.json')}")
    print("\nUntil a Buildkite token is in devreap's config, the build columns are")
    print("blank — devdash reads thresholds and the Buildkite token from devreap.")


if __name__ == "__main__":
    main()
