"""The screen-lock probe.

The parsing is tested against a real (trimmed) `ioreg -n Root -d1` dump rather
than a synthetic one, because the whole point is that these keys really do sit
in that shape. The subprocess is stubbed, so the suite never reads the
machine's actual lock state.
"""

import subprocess

import pytest

from devdash import screen

# The tail of a real dump, unlocked: IOConsoleLocked No, the session dict with
# no CGSSessionScreenIsLocked key at all.
UNLOCKED = """
    "IOConsoleLocked" = No
    "IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes,"kCGSessionUserNameKey"="nick","kCGSessionLoginDoneKey"=Yes})
"""

# The same dump while locked: a top-level IOConsoleLocked Yes, and the key
# inside the session dict.
LOCKED = """
    "IOConsoleLocked" = Yes
    "IOConsoleUsers" = ({"kCGSSessionOnConsoleKey"=Yes,"CGSSessionScreenIsLocked"=Yes,"kCGSessionUserNameKey"="nick"})
"""


def test_an_unlocked_dump_is_not_locked():
    assert screen._parse(UNLOCKED) is False


def test_a_locked_dump_is_locked():
    assert screen._parse(LOCKED) is True


def test_either_key_alone_is_enough():
    # IOConsoleLocked on its own.
    assert screen._parse('"IOConsoleLocked" = Yes') is True
    # CGSSessionScreenIsLocked on its own, nested as it really appears.
    assert screen._parse('{"CGSSessionScreenIsLocked"=Yes}') is True


def test_the_key_must_say_yes_not_merely_appear():
    # The value, not the name, is the signal.
    assert screen._parse('"CGSSessionScreenIsLocked" = No') is False


@pytest.mark.parametrize("text", ["", None])
def test_an_empty_dump_is_not_locked(text):
    assert screen._parse(text) is False


def test_locked_reads_the_dump(monkeypatch):
    monkeypatch.setattr(screen.subprocess, "run",
                        lambda *a, **kw: subprocess.CompletedProcess(a, 0, LOCKED, ""))
    assert screen.locked() is True


def test_a_failed_probe_is_not_locked(monkeypatch):
    def boom(*a, **kw):
        raise OSError("no ioreg")

    monkeypatch.setattr(screen.subprocess, "run", boom)
    assert screen.locked() is False
