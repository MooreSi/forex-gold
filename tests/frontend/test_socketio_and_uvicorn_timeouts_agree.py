"""The two keepalive layers must not disagree, or the tighter one wins.

**The owner's symptom, reported 2026-09-10:** *"often if I haven't been using
the app for a while and come back to the web page it reloads"* — and that
reload costs a ~2s event-loop stall, because every panel re-renders in one tick
(bugs/030 cause 3).

`run.py` already tuned the keepalive for exactly this, with a comment saying
browsers throttle background tabs. It tuned **uvicorn's** websocket ping:

    ws_ping_interval=30
    ws_ping_timeout=60      # allow 60 s for a pong before closing

But NiceGUI's transport is socket.io, and `nicegui.nicegui` derives engine.io's
own timings from a different parameter:

    sio.eio.ping_interval = max(reconnect_timeout * 0.8, 4)
    sio.eio.ping_timeout  = max(reconnect_timeout * 0.4, 2)

At `reconnect_timeout=30` that is a ping every 24 s with a **12 s** pong
window. uvicorn tolerates 60 s; engine.io gives up at 12. **The tighter one
decides**, so the deliberate 60 s never applied, and a backgrounded tab —
throttled by the browser to a timer a minute or worse — misses the deadline,
the session is dropped, and the client rebuilds the whole page.

This pins the two layers to agree. It does not pin a specific number: if
someone lowers `ws_ping_timeout`, that is fine, so long as engine.io is not
the one silently deciding.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

RUN_PY = Path(__file__).resolve().parents[2] / "run.py"


def _ui_run_kwargs() -> dict[str, float]:
    """The literal numeric kwargs passed to ui.run(), read from the source."""
    tree = ast.parse(RUN_PY.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "run"
                and getattr(node.func.value, "id", "") == "ui"):
            out = {}
            for kw in node.keywords:
                if isinstance(kw.value, ast.Constant) and isinstance(
                        kw.value.value, (int, float)):
                    out[kw.arg] = float(kw.value.value)
            return out
    raise AssertionError("no ui.run(...) call found in run.py")


def _eio_ping_timeout(reconnect_timeout: float) -> float:
    """NiceGUI's own derivation, mirrored from nicegui.nicegui."""
    return max(reconnect_timeout * 0.4, 2)


def _eio_ping_interval(reconnect_timeout: float) -> float:
    return max(reconnect_timeout * 0.8, 4)


class TestTheLayersAgree:
    def test_engine_io_is_not_the_tighter_deadline(self):
        kw = _ui_run_kwargs()
        rt = kw["reconnect_timeout"]
        ws = kw["ws_ping_timeout"]

        assert _eio_ping_timeout(rt) >= ws, (
            f"reconnect_timeout={rt:.0f} gives engine.io a "
            f"{_eio_ping_timeout(rt):.0f}s pong window, tighter than the "
            f"{ws:.0f}s ws_ping_timeout deliberately set beside it — so the "
            f"60s is decorative and engine.io drops the session first"
        )

    def test_the_pong_window_survives_a_throttled_background_tab(self):
        """Browsers throttle a backgrounded tab's timers to roughly once a
        minute. A window under that guarantees the reload the owner sees."""
        rt = _ui_run_kwargs()["reconnect_timeout"]

        assert _eio_ping_timeout(rt) >= 60, (
            f"a {_eio_ping_timeout(rt):.0f}s pong window is shorter than "
            f"background-tab throttling, so an idle tab is dropped and rebuilt"
        )


class TestTheDerivationStillMatchesNiceGUI:
    """If NiceGUI changes how it derives these, the arithmetic above is wrong
    and this file would be reasoning about a formula that no longer exists."""

    def test_nicegui_still_derives_them_from_reconnect_timeout(self):
        import inspect
        from nicegui import nicegui as ng

        src = inspect.getsource(ng)
        assert "eio.ping_timeout" in src and "reconnect_timeout" in src, (
            "NiceGUI no longer derives engine.io's ping timings from "
            "reconnect_timeout — re-check this file's arithmetic"
        )
