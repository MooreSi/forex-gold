"""NiceGUI-internals canary (stage2 phase4/030).

frontend/app.py monkey-patches NiceGUI internals (the timer-teardown fix
and the websocket buffer size). Those patches were written against 3.12.x;
the installed version is already newer. If an upgrade removes or renames
any patched attribute, the app breaks silently at runtime — this canary
makes the break loud at test time instead.

Nothing here can reach a broker.
"""
from __future__ import annotations


def test_patched_timer_internals_exist():
    """app.py replaces these two methods; they must exist to be replaced,
    and the names the replacements call must exist to be called."""
    from nicegui.elements.timer import Timer as UITimer
    from nicegui.timer import Timer as BaseTimer

    assert callable(getattr(UITimer, "_get_context", None)), \
        "Timer._get_context gone — the teardown patch no longer applies"
    assert callable(getattr(BaseTimer, "_cleanup", None)), \
        "Timer._cleanup gone — the teardown patch no longer applies"
    # The replacement bodies reach these attributes:
    assert hasattr(UITimer, "parent_slot") or hasattr(BaseTimer, "parent_slot"), \
        "parent_slot property gone — _safe_timer_get_context would AttributeError"


def test_patched_socketio_buffer_attribute_exists():
    """app.py sets core.sio.eio.max_http_buffer_size = 10MB at import."""
    from nicegui import core

    assert hasattr(core, "sio"), "nicegui.core.sio gone"
    assert hasattr(core.sio, "eio"), "core.sio.eio gone"
    assert hasattr(core.sio.eio, "max_http_buffer_size"), \
        "max_http_buffer_size gone — the 10MB websocket patch silently stops applying"


def test_the_buffer_patch_actually_applied():
    """Importing the shell must leave the raised limit in place — this is
    the live check that the patch still lands on the running object."""
    import frontend.app  # noqa: F401  (applies the patch at import)
    from nicegui import core

    assert core.sio.eio.max_http_buffer_size == 10_000_000
