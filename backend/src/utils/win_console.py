"""Turn QuickEdit off in this process's Windows console.

With QuickEdit on (the Windows default), selecting text in a console window
suspends every write to that console until the selection is cleared. The
console log handler writes from the event-loop thread, so selecting a line to
copy it froze the whole app: on the VPS, 2026-09-28, two stalls of 13.7 s and
15.5 s were sampled inside logging's `emit`, and each dropped the EA link.

Copying and right-click paste still work with QuickEdit off; selecting starts
from the window menu's Edit > Mark instead of a plain drag.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from typing import Any, Optional

log = logging.getLogger(__name__)

_STD_INPUT_HANDLE = -10
_ENABLE_QUICK_EDIT_MODE = 0x0040
_ENABLE_EXTENDED_FLAGS = 0x0080


def disable_quick_edit(kernel32: Optional[Any] = None,
                       platform: Optional[str] = None) -> bool:
    """Clear QuickEdit on the console's input handle. True if it was changed.

    A no-op off Windows, with no console (pythonw, a redirected stdin), or
    when it is already off. Never raises: a console setting is not a reason
    for the app not to start.
    """
    if (platform or sys.platform) != "win32":
        return False
    try:
        k32 = kernel32 if kernel32 is not None else ctypes.windll.kernel32
        handle = k32.GetStdHandle(_STD_INPUT_HANDLE)
        mode = ctypes.c_uint32()
        if not k32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        if not mode.value & _ENABLE_QUICK_EDIT_MODE:
            return False
        new = (mode.value & ~_ENABLE_QUICK_EDIT_MODE) | _ENABLE_EXTENDED_FLAGS
        if not k32.SetConsoleMode(handle, new):
            return False
    except Exception as e:
        log.debug("[Console] could not turn QuickEdit off: %s", e)
        return False
    log.info("[Console] QuickEdit turned off: selecting text in this window no "
             "longer pauses the app (Edit > Mark still selects)")
    return True
