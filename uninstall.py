#!/usr/bin/env python3
"""Install/uninstall parity: the agent devdash installs is the only way it is
meant to run in production, so the removal path has to be as easy as the
install.

Run it standalone: python3 uninstall.py
"""

import os
import subprocess

LABEL = "com.nickbrett1.devdash"


def main():
    uid = os.getuid()
    dest = os.path.expanduser(f"~/Library/LaunchAgents/{LABEL}.plist")

    r = subprocess.run(["launchctl", "bootout", f"gui/{uid}/{LABEL}"],
                       capture_output=True, text=True, check=False)
    print("LaunchAgent unloaded." if r.returncode == 0 else "LaunchAgent was not loaded.")

    if os.path.exists(dest):
        os.remove(dest)
        print(f"Removed {dest}")

    print("\nKept (delete by hand if you want them gone):")
    print("  ~/.devdash/config.json   — the token; deleting it mints a new one")
    print("  the checkout and its .venv")


if __name__ == "__main__":
    main()
