"""Engine panels' API -- shared by the Breakout and Reversal panels.

The panels used to reach `run_db(fn)` here, handing this layer an arbitrary
callable to run on the DB worker thread. That inverted the dependency: the
page chose the data access and the controller just dispatched it. Each of
those calls is now a named function on the owning engine's service.
"""
from __future__ import annotations

from typing import Any

from backend.src.services.breakout_signal import panel_data as breakout
from backend.src.services.cluster import remote_control as _remote
from backend.src.services.engines import registry as _engines
from backend.src.services.reversal_engine import panel_data as reversal
from backend.src.services.reversal_engine import reversal_engine_service as _re_svc
from backend.src.services.risk import settings as _risk

__all__ = [
    "breakout", "reversal", "get_risk_settings", "get_risk_settings_async",
    "update_risk_settings", "get_engine", "engines_running", "sub_engines",
    "ENGINE_NAMES", "control_target", "effective_settings",
    "set_engine_running", "set_ai_eval", "AI_EVAL_KEYS",
    "RemoteControlFailed", "reversal_realised_pnl", "pro_model_status",
    "pro_model_fit_in_background",
    "reversal_research_study", "reversal_shadow_report",
    "reversal_ai_recommend", "reversal_ai_apply",
    "reversal_reset_stats",
]


def get_risk_settings() -> dict:
    return _risk.get()


async def get_risk_settings_async() -> dict:
    return await _risk.get_async()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


# ── Engine lifecycle (restructure phase1/010) ────────────────────────────────
# Named operations, not re-exported singletons, so no page loops over engines
# choosing lifecycle again. The table and the BULK loops are in
# services/engines/registry.py and deliberately not re-exported: their only
# caller is the handover, a service, which may not import a controller.


# Which engines exist, in the fixed binding order. Re-exported so a router can
# render the tab from it rather than restating the list and drifting.
ENGINE_NAMES = _engines.ENGINE_NAMES


def get_engine(name: str) -> Any:
    """The named engine's live instance (its panel needs status attributes
    and its refresh-callback hook)."""
    return _engines.instance(name)


def engines_running() -> dict:
    return _engines.running()


def sub_engines() -> tuple:
    """(breakout, bounce, reversal) instances in the fixed binding order the
    sync server's server_start has always received them."""
    return _engines.all_instances()


# ── Acting on the node that is actually trading ──────────────────────────────
# With the VPS trading, this machine's engines are stood down, so a Start/Stop
# here "does nothing useful while looking like it worked" (the sync server's
# own words). remote_control.py decides where a control lands.

AI_EVAL_KEYS = _remote.AI_EVAL_KEYS
RemoteControlFailed = _remote.RemoteControlFailed


def control_target() -> str:
    """"local", "remote" or "centralized" -- which node a control will reach."""
    return _remote.where()


def effective_settings(local: dict) -> dict:
    """The settings the engines are actually obeying."""
    return _remote.effective_settings(local)


async def set_engine_running(*args, **kwargs) -> dict:
    return await _remote.set_engine_running(*args, **kwargs)


async def set_ai_eval(*args, **kwargs) -> dict:
    return await _remote.set_ai_eval(*args, **kwargs)


async def place_market_order(*args, **kwargs) -> dict:
    """Place a market order on whichever node is actually trading."""
    return await _remote.place_market_order(*args, **kwargs)


async def reversal_realised_pnl() -> dict:
    """The Reversal Engine's REAL closed P&L -- the trades it actually placed,
    read from the core trade ledger rather than the engine's own virtual one."""
    return await reversal.get_realised_pnl()


# ── Reversal Engine: the pro-likeness sub-model ──────────────────────────────

def pro_model_status() -> dict:
    """Whether the model is fitted and usable, and why not if it is not."""
    from backend.src.services.reversal_engine import pro_model as _pm
    return _pm.status()


# `pro_model_fit` -- the BLOCKING refit -- is deliberately NOT here. It stops
# the event loop for about five seconds, which takes the EA socket reader and
# the monitor loop with it (bugs/030), so no router may call it and a
# controller operation exists for a router. `pro_model_fit_in_background` is
# the one this layer offers; services/reversal_engine/pro_model.fit is still
# there for the command line.


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


# `reversal_macro_backfill` is a manual repair tool, not a screen: applying it
# changes what the ML gate learns at its next retrain. It lives in
# services/reversal_engine/macro_backfill.py and is run deliberately.


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

