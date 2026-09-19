"""Why a backtest produced no numbers for a strategy.

**Zeros are not a neutral answer in a comparison table.** "0 trades, 0 loss"
sits beside a row showing a real drawdown and reads as the safer choice --
an argument FOR the strategy that was never tested at all. So every refusal
here is a sentence, and `run_backtest` puts it on the result.

The template half has existed since the template walk was written. The
built-in half was added 2026-09-19, after the owner reported that the backtest
"doesn't work": six built-in strategies -- including `fixed_rr`, the picker's
first option and the baseline every other row is read against -- had no
dispatch branch and returned zeros in silence.

Its own module rather than more of engine.py: this is not simulation, and
engine.py sits at the 800-line ceiling. `engine` is imported lazily inside the
functions because engine imports this at load time.
"""
from __future__ import annotations

# `why` alone. EA_MANAGED is the table `builtin` reads, not a surface anything
# outside this module is meant to consume -- a declared export is a statement
# about what a module is FOR.
__all__ = ["why"]


# Strategies whose management lives in the Expert Advisor's own Manage*
# routines rather than in any Python this walk could mirror. Re-deriving them
# from the EA's source would produce plausible numbers used to decide what
# trades real money, so the walk refuses and says so. Implementing them
# faithfully is an owner decision -- docs/simon-handover/.
EA_MANAGED: dict[str, str] = {
    "scalp_runner": "Scalp Runner",
    "gold_diggers_copy": "Gold Diggers Copy",
    "orb_fixed": "ORB Fixed",
    "adaptive_runner_2": "Adaptive Runner 2",
    "limit_runner": "Limit Runner",
}


def builtin(strategy: str, tick_mode: bool = False) -> str:
    """Why this walk will not model a built-in strategy, or "" when it will.

    Added 2026-09-19. Six built-in strategies had no dispatch branch and so
    returned zero trades with no explanation -- including `fixed_rr`, the
    picker's first option and the baseline every other row is compared
    against. Zeros are not neutral in a comparison table: "0 trades, 0 loss"
    beside a row showing a real drawdown reads as the safer choice, which is
    an argument FOR the strategy that was never tested. The template walk has
    understood this since it was written; the built-ins were never held to it.
    """
    from backend.src.services.backtest.engine import TEMPLATE_PREFIX

    if strategy.startswith(TEMPLATE_PREFIX):
        return ""

    if tick_mode:
        # `_simulate_ticks` returns None for EVERY built-in, not just the
        # EA-managed ones: its docstring says the picker "has offered EA
        # templates exclusively", which was true of the NiceGUI page and is
        # not true of the React form. Blaming the strategy would be wrong --
        # Scale Out walks perfectly well on candles. What cannot be done is
        # walking it on ticks.
        return (
            "The tick walk models EA templates only. This built-in strategy "
            "is managed by this app bar by bar, and the tick series carries "
            "no bars to manage it against — run it on Candles."
        )

    label = EA_MANAGED.get(strategy)
    if not label:
        return ""
    return (
        f"{label} is managed by the EA on the MT5 side, not by this app, so "
        f"this walk has no Python rules to mirror. A plausible number here "
        f"would be used to choose what trades real money, so it reports "
        f"nothing instead."
    )


def template(strategy: str, tick_mode: bool) -> str:
    """Why this walk refused `strategy`'s template, or "" when it did not.

    Deliberately narrow. A built-in strategy is not "unsupported" -- it is
    simply not walked on ticks, a different silence. A template that no longer
    exists is a different problem again, and labelling it unsupported sends
    the user to edit a trail mode on a template that is not there. Only a
    template the walk actively refuses gets a reason.
    """
    from backend.src.services.backtest.engine import (
        TEMPLATE_PREFIX, _load_backtest_template,
    )

    if not strategy.startswith(TEMPLATE_PREFIX):
        return ""
    loaded = _load_backtest_template(strategy[len(TEMPLATE_PREFIX):])
    if not loaded:
        return ""
    try:
        from backend.src.services.backtest.template_simulator import (
            unsupported_reason as _why,
        )
        # The bar walk can always compute an ATR at the fill; the tick
        # walk has no candle series to derive one from.
        return _why(loaded, tick_mode, atr_available=not tick_mode)
    except Exception:
        return ""


def why(strategy: str, tick_mode: bool) -> str:
    """The one call `run_backtest` makes. Built-in reasons first: a built-in
    strategy is never a template, and asking the template loader about one
    would send the operator to edit a template that does not exist."""
    return builtin(strategy, tick_mode) or template(strategy, tick_mode)
