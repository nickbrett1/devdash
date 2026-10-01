"""Is the Mac's screen locked?

This exists because the window count lies while the screen is locked. System
Events lists every process, returns an empty window list for each of them, and
does so *without an error* — so "0 vscode windows" means either "nothing is
open" or "nobody can see the screen", and the page cannot tell those apart from
the count alone. That is the difference between a normal page and one that
looks broken after a successful Open.

`ioreg` answers it directly, in ~50ms.
"""

import re
import subprocess

_IOREG = "/usr/sbin/ioreg"

# Both spellings appear in the same ioreg dump: IOConsoleLocked at the top
# level, CGSSessionScreenIsLocked inside the IOConsoleUsers dict. Either one
# saying Yes means the console is locked.
_LOCK_KEYS = ("CGSSessionScreenIsLocked", "IOConsoleLocked")


def locked():
    """True when the console session is locked.

    Never raises. A probe that cannot run means "not locked" — the window count
    is then the only signal, which is exactly the old behaviour, and a missing
    ioreg must not take the page down with it.
    """
    try:
        r = subprocess.run(
            [_IOREG, "-n", "Root", "-d1"],
            capture_output=True, text=True, timeout=5, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return _parse(r.stdout)


def _parse(text):
    """True if any lock key reads Yes. Split out so it can be tested without
    shelling out to ioreg."""
    for key in _LOCK_KEYS:
        if re.search(rf'"{key}"\s*=\s*Yes', text or ""):
            return True
    return False
