"""Engine panels' API -- shared by the Breakout and Reversal panels.

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

__all__ = ["breakout", "reversal",
           "get_risk_settings", "get_risk_settings_async", "update_risk_settings",
           "get_engine", "engines_running", "sub_engines",
           "start_stopped_engines", "stop_running_engines"]


# The signal engines by name, in the fixed (breakout, bounce, reversal) order
# the mode toggle and the sync server have always bound them. Bounce's code was
# deleted on 2026-09-14; its NAME stays because dropping the slot would shift
# Reversal into its position on a paired node still running the old build.
_ENGINE_SERVICES = {
    "breakout": _bo_svc,
    "bounce": None,
    "reversal": _re_svc,
}


def _instance(svc) -> Any:
    """An engine, or None -- for an empty slot as much as an unbuilt one."""
    return svc.get_instance() if svc is not None else None


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
    return _instance(_ENGINE_SERVICES[name])


def engines_running() -> dict:
    return {
        name: bool(getattr(_instance(svc), "is_running", False))
        for name, svc in _ENGINE_SERVICES.items()
    }


def sub_engines() -> tuple:
    """(breakout, bounce, reversal) instances in the fixed binding order the
    sync server's server_start has always received them."""
    return tuple(_instance(svc) for svc in _ENGINE_SERVICES.values())


# Belt and braces: the slot is empty, so the loop would skip it anyway. The
# exclusion keeps the safety property asserted rather than incidental.
_NOT_BULK_STARTED = ("bounce",)


def start_stopped_engines() -> None:
    for name, svc in _ENGINE_SERVICES.items():
        if name in _NOT_BULK_STARTED:
            continue
        eng = _instance(svc)
        if eng is not None and not getattr(eng, "is_running", False):
            eng.start()


def stop_running_engines() -> None:
    for svc in _ENGINE_SERVICES.values():
        eng = _instance(svc)
        if eng is not None and getattr(eng, "is_running", False):
            eng.stop()


async def reversal_realised_pnl() -> dict:
    """The Reversal Engine's REAL closed P&L -- the trades it actually placed,
    read from the core trade ledger rather than the engine's own virtual one."""
    return await reversal.get_realised_pnl()


# ── Reversal Engine: the pro-likeness sub-model ──────────────────────────────

def pro_model_status() -> dict:
    """Whether the model is fitted and usable, and why not if it is not."""
    from backend.src.services.reversal_engine import pro_model as _pm
    return _pm.status()


def pro_model_fit(*args, **kwargs):
    """Refit from the captured corpus. Expensive; the panel offers it as an
    explicit button rather than running it on render.

    BLOCKS for about five seconds on the live corpus. Anything running on the
    event loop wants pro_model_fit_in_background instead (bugs/030)."""
    from backend.src.services.reversal_engine import pro_model as _pm
    return _pm.fit(*args, **kwargs)


def pro_model_fit_in_background(force: bool = False) -> None:
    """Start a refit and return at once.

    For UI handlers: they run on the shared asyncio loop, so a synchronous fit
    there freezes the EA socket reader and the monitor loop too, not just the
    page that asked for it."""
    from backend.src.services.reversal_engine import pro_model as _pm
    _pm.fit_in_background(force=force)


async def reversal_research_study(**kwargs) -> str:
    """Run the reversal engine's phase-1 research study and render it.

    Reads history, writes two measurement columns, places nothing. See
    services/reversal_engine/research_lab.py and
    docs/todo/reversal-engine/200.

    The bridge comes from the running engine rather than the caller: the
    study needs the same broker connection the engine trades on, and a UI
    that had to find one would be reaching past this layer to do it.
    """
    from backend.src.services.reversal_engine import research_lab as _lab
    engine = _re_svc.get_instance()
    bridge = getattr(engine, "_bridge", None) if engine else None
    if bridge is None:
        return ("The reversal engine is not running, so there is no broker "
                "connection to read history through. Start it and try again.")
    return _lab.render(await _lab.run_study(bridge, **kwargs))


def reversal_shadow_report() -> list:
    """Champion vs challenger, over the signals both have seen."""
    from backend.src.services.reversal_engine import shadow as _shadow
    return _shadow.report()


def reversal_macro_backfill(apply: bool = False) -> dict:
    """Repair the macro features of stored training vectors.

    `apply=False` reports what would change and writes nothing. Applying it
    changes what the ML gate learns at its next retrain, so the default is
    the report. See services/reversal_engine/macro_backfill.py.
    """
    from backend.src.services.reversal_engine import macro_backfill as _mb
    return _mb.run(apply=apply)


async def reversal_ai_recommend() -> dict:
    """Ask the configured AI for capability settings, using the measured
    evidence. Writes nothing -- the caller decides whether to apply it."""
    from backend.src.services.reversal_engine import ai_tuner as _tuner
    engine = _re_svc.get_instance()
    bridge = getattr(engine, "_bridge", None) if engine else None
    return await _tuner.recommend(bridge, get_risk_settings())


def reversal_ai_apply(settings: dict) -> dict:
    """Write a recommendation the user has accepted.

    Re-sanitised here rather than trusted: what reaches this function has
    been through a UI and back, and the allowlist is the only thing
    standing between a model's output and a live trading setting.
    """
    from backend.src.services.reversal_engine import ai_tuner as _tuner
    clean = _tuner.sanitise(settings)
    if clean:
        update_risk_settings(clean)
    return clean


async def reversal_reset_stats() -> float:
    """Start the Reversal Engine panel's numbers again from now.

    Reporting only: no signal row, stored feature vector, reconstructed
    excursion or attribution history is removed. See
    services/reversal_engine/stats_repo.reset_stats.
    """
    return await reversal.reset_stats()

