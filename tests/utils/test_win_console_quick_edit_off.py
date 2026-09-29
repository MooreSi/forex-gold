"""The app turns QuickEdit off in its Windows console at startup.

With QuickEdit on (the Windows default), selecting text in a console window
suspends every write to that console until the selection is cleared. The
console log handler writes from the event-loop thread, so the whole app stops:
on the VPS, 2026-09-28, two stalls of 13.7 s and 15.5 s were both sampled
inside logging's `emit`, and each dropped the EA link ("send failed:
Connection lost"). Copying lines out of the VPS console is exactly what the
owner does to report a fault.

Nothing here touches a real console: kernel32 is a recorder.
"""
from __future__ import annotations

import inspect

import pytest

from backend.src.utils import win_console

QUICK_EDIT = 0x0040
EXTENDED_FLAGS = 0x0080
INSERT_MODE = 0x0020


class _Kernel32:
    def __init__(self, mode, get_ok=True):
        self.mode = mode
        self.get_ok = get_ok
        self.set_to = None

    def GetStdHandle(self, which):
        assert which == -10, "QuickEdit is a property of the INPUT handle"
        return 7

    def GetConsoleMode(self, handle, ref):
        if not self.get_ok:
            return 0
        ref._obj.value = self.mode
        return 1

    def SetConsoleMode(self, handle, mode):
        self.set_to = mode
        return 1


def test_quick_edit_is_cleared_and_everything_else_kept():
    k = _Kernel32(QUICK_EDIT | EXTENDED_FLAGS | INSERT_MODE)

    assert win_console.disable_quick_edit(kernel32=k, platform="win32") is True

    assert k.set_to & QUICK_EDIT == 0
    assert k.set_to & INSERT_MODE, "other input flags must survive"
    assert k.set_to & EXTENDED_FLAGS, "without EXTENDED_FLAGS the change is ignored"


def test_an_already_off_console_is_left_alone():
    k = _Kernel32(EXTENDED_FLAGS | INSERT_MODE)

    assert win_console.disable_quick_edit(kernel32=k, platform="win32") is False
    assert k.set_to is None


def test_no_console_at_all_is_not_an_error():
    """pythonw, a service, a redirected stdin: GetConsoleMode fails."""
    k = _Kernel32(0, get_ok=False)

    assert win_console.disable_quick_edit(kernel32=k, platform="win32") is False
    assert k.set_to is None


def test_off_windows_it_does_nothing():
    k = _Kernel32(QUICK_EDIT)

    assert win_console.disable_quick_edit(kernel32=k, platform="darwin") is False
    assert k.set_to is None


def test_the_app_calls_it_at_startup():
    import run
    assert "disable_quick_edit" in inspect.getsource(run.main)
