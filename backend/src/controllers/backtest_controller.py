"""The backtest engine, as its page calls it.

Forwards to backend.src.services.backtest.engine unchanged. Everything here
is simulation over stored history -- it places nothing and touches no broker.
"""
from __future__ import annotations

from backend.src.services.backtest import engine as _engine

__all__ = [
    "run_backtest", "signals_from_db", "filter_signals",
    "BtSignal", "StrategyStats", "FilterStats", "BROKER_TZ_OFFSET",
]

BtSignal = _engine.BtSignal
StrategyStats = _engine.StrategyStats
FilterStats = _engine.FilterStats

# The broker's clock offset, which the page needs to label result timestamps
# in the same timezone the trades were recorded in.
BROKER_TZ_OFFSET = _engine._BROKER_TZ_OFFSET


def run_backtest(*args, **kwargs):
    return _engine.run_backtest(*args, **kwargs)


def signals_from_db(*args, **kwargs):
    return _engine.signals_from_db(*args, **kwargs)


def filter_signals(*args, **kwargs):
    return _engine.filter_signals(*args, **kwargs)
