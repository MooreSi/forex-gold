"""The Reversal Engine's own panel: its P&L, its ML gate, its virtual trades.

Split out of `engines_controller` on 2026-09-19. That file runs the engine
LIFECYCLE — which engines exist, starting and stopping them, and routing a
control to whichever node is actually trading — and had grown a second job:
everything the Reversal Engine specifically measures about itself. Adding the
virtual trade history took it past the 200-line controller ceiling, and the
gate's message is the right diagnosis: a controller that long is holding more
than one thing.

Nothing moved but its address. Every operation here is the same forwarder it
was, in the same order, with the same comments.
"""
from __future__ import annotations

from backend.src.services.reversal_engine import panel_data as reversal
from backend.src.services.reversal_engine import reversal_engine_service as _re_svc
from backend.src.services.risk import settings as _risk

__all__ = [
    "reversal", "reversal_realised_pnl", "reversal_edge_stats", "pro_model_status",
    "pro_model_fit_in_background", "reversal_research_study",
    "reversal_shadow_report", "reversal_shadow_history",
    "reversal_ai_recommend", "reversal_ai_apply", "reversal_reset_stats",
    "reversal_ml_metrics", "reversal_ml_summary",
]


def get_risk_settings() -> dict:
    return _risk.get()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


async def reversal_edge_stats() -> dict:
    """Profit factor and expectancy over the trades this engine really placed.

    A win rate on its own decides nothing: 38% with an average win three times
    the average loss is profitable, and 60% with the ratio inverted is not.
    """
    return await reversal.get_edge_stats()


async def reversal_realised_pnl() -> dict:
    """The Reversal Engine's REAL closed P&L -- the trades it actually placed,
    read from the core trade ledger rather than the engine's own virtual one."""
    return await reversal.get_realised_pnl()


# ── Reversal Engine: the pro-likeness sub-model ──────────────────────────────

async def reversal_ml_metrics() -> dict:
    """The learning curve: per-signal win flags and realised R, oldest first.

    Raw outcomes rather than a running average, because the panel rolls them
    over its own window and a cumulative mean cannot be un-averaged back.
    """
    return await reversal.ml_metrics()


async def reversal_ml_summary() -> dict:
    """Whether the ML gate is trained, and how far off it is if not."""
    return await reversal.ml_summary()


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


def reversal_shadow_history(limit: int = 100) -> list:
    """The virtual trade ledger: every variant's call, newest first."""
    from backend.src.services.reversal_engine import shadow as _shadow
    return _shadow.history(limit)


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

