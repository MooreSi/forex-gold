"""The Breakout engine's own panel: its balance, its stats, its ML gate.

`services/breakout_signal/panel_data.py` has declared seventeen named
operations since the restructure, and until 2026-09-19 **nothing called any of
them**. `engines_controller` re-exported the module as `breakout` and the
React Signal Generator tab showed the Reversal engine's ML gate and virtual
trades in detail while saying nothing about Breakout beyond a Start/Stop card.

Its own module rather than more of `engines_controller`, for the reason the
2026-09-19 reversal split already gives: that file runs the engine lifecycle,
and one engine's measurements are a different job.

Every read is individually guarded in the router, not here. The engine keeps
its own database and a fresh install has none.
"""
from __future__ import annotations

from backend.src.services.breakout_signal import panel_data as _panel

__all__ = [
    "breakout_adaptive_params", "breakout_analysis_log",
    "breakout_recent_signals",
    "breakout_edge_stats", "breakout_stats", "breakout_virtual_balance", "breakout_max_drawdown",
    "breakout_ml_summary", "breakout_ml_metrics", "breakout_ml_thresholds",
    "breakout_perf_by_session", "breakout_perf_by_adx_band",
    "breakout_perf_by_type", "breakout_perf_by_bias",
]


async def breakout_edge_stats() -> dict:
    """Profit factor, expectancy, and the averages behind them.

    A win rate on its own decides nothing: 38% with an average win three times
    the average loss is a profitable engine, and 60% with the ratio inverted
    is not.
    """
    return await _panel.edge_stats()


async def breakout_stats() -> dict:
    """Closed-trade totals: count, wins, win rate, net P&L."""
    return await _panel.stats()


async def breakout_virtual_balance():
    """The engine's own paper balance, independent of the account."""
    return await _panel.virtual_balance()


async def breakout_max_drawdown():
    """Worst peak-to-trough fall on that paper balance."""
    return await _panel.max_drawdown()


async def breakout_ml_summary() -> dict:
    """Whether the classifier is trained, and on how much."""
    return await _panel.ml_summary()


async def breakout_ml_metrics() -> dict:
    """How well it is calibrated: Brier score, rolling MCC, the series behind
    them."""
    return await _panel.ml_metrics()


def breakout_ml_thresholds() -> dict:
    """What "trained" is measured against. Without it, a labelled count is a
    number with nothing to compare to."""
    return _panel.ml_thresholds()


async def breakout_perf_by_session() -> list:
    """P&L by trading session -- the split that exposed this engine's
    12:00-15:00 UTC losses, which hid in the aggregate for weeks."""
    return await _panel.perf_by_session()


async def breakout_perf_by_adx_band() -> list:
    """P&L by ADX band: whether the engine needs a trend to work."""
    return await _panel.perf_by_adx_band()


async def breakout_perf_by_type() -> list:
    """P&L by the kind of breakout that triggered it."""
    return await _panel.perf_by_breakout_type()


async def breakout_perf_by_bias() -> list:
    """P&L split by whether the trade agreed with the higher-timeframe bias."""
    return await _panel.perf_by_bias()


async def breakout_recent_signals(limit: int = 25) -> list:
    """The engine's own signals, newest first."""
    return await _panel.all_signals(limit)


async def breakout_analysis_log(limit: int = 25) -> list:
    """Why the engine acted, or did not.

    The only read in this app that answers the second half. Every other panel
    says what an engine DID; a suppressed candidate leaves no trade behind to
    look at, so without this the operator cannot tell a quiet market from a
    gate that is set too tight.
    """
    return await _panel.analysis_log(limit)


async def breakout_adaptive_params() -> dict:
    """The parameters the engine has tuned for itself, with their defaults.

    Shown against the default rather than alone: a threshold that has drifted
    is the engine telling you something, and a number with nothing to compare
    it to is not.
    """
    return await _panel.adaptive_params()
