"""Engine panels' API -- shared by the Breakout, Reversal and Bounce panels.

The panels used to reach `run_db(fn)` here, handing this layer an arbitrary
callable to run on the DB worker thread. That inverted the dependency: the
page chose the data access and the controller just dispatched it. Each of
those calls is now a named function on the owning engine's service.
"""
from __future__ import annotations

from typing import Any

from backend.src.services.breakout_signal import breakout_signal_service as _bo_svc
from backend.src.services.breakout_signal import panel_data as breakout
from backend.src.services.reversal_engine import panel_data as reversal
from backend.src.services.reversal_engine import reversal_engine_service as _re_svc
from backend.src.services.risk import settings as _risk
from backend.src.services.test_signal import panel_data as bounce
from backend.src.services.test_signal import test_signal_service as _bc_svc

__all__ = ["breakout", "reversal", "bounce",
           "get_risk_settings", "get_risk_settings_async", "update_risk_settings",
           "get_engine", "engines_running", "sub_engines",
           "start_stopped_engines", "stop_running_engines"]

# The three signal engines by their user-facing names, in the fixed
# (breakout, bounce, reversal) order the mode toggle and the sync server
# have always bound them.
_ENGINE_SERVICES = {
    "breakout": _bo_svc,
    "bounce": _bc_svc,
    "reversal": _re_svc,
}


def get_risk_settings() -> dict:
    return _risk.get()


async def get_risk_settings_async() -> dict:
    return await _risk.get_async()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


# ── Engine lifecycle (restructure phase1/010) ────────────────────────────────
# Named operations instead of re-exported singletons, so no page loops over
# engines choosing lifecycle again. The only-if-not-running guard below is
# the documented mode-toggle semantics moved verbatim from frontend/app.py.


def get_engine(name: str) -> Any:
    """The named engine's live instance (its panel needs status attributes
    and its refresh-callback hook)."""
    return _ENGINE_SERVICES[name].get_instance()


def engines_running() -> dict:
    return {
        name: bool(getattr(svc.get_instance(), "is_running", False))
        for name, svc in _ENGINE_SERVICES.items()
    }


def sub_engines() -> tuple:
    """(breakout, bounce, reversal) instances in the fixed binding order the
    sync server's server_start has always received them."""
    return tuple(svc.get_instance() for svc in _ENGINE_SERVICES.values())


def start_stopped_engines() -> None:
    for svc in _ENGINE_SERVICES.values():
        eng = svc.get_instance()
        if eng is not None and not getattr(eng, "is_running", False):
            eng.start()


def stop_running_engines() -> None:
    for svc in _ENGINE_SERVICES.values():
        eng = svc.get_instance()
        if eng is not None and getattr(eng, "is_running", False):
            eng.stop()


async def reversal_realised_pnl() -> dict:
    """The Reversal Engine's REAL closed P&L -- the trades it actually placed,
    read from the core trade ledger rather than the engine's own virtual one."""
    return await reversal.get_realised_pnl()
