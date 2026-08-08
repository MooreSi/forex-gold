"""Panel reads for the Breakout signal-generator page.

Every function here is one named operation the panel performs, dispatched off
the event loop. This module exists to kill `run_db(fn)` -- a controller hatch
that took an arbitrary callable from the page and ran it on the DB worker
thread. That inverted the layering: the *page* chose the data access and the
controller only supplied a thread. It also meant every panel imported this
engine's repo directly to have something to hand over.

The local/remote facade is built here rather than in the page. In Remote mode
(the VPS is the active trader) these transparently read the mirrored remote
signal-gen stats instead of this node's own local data -- see
`cluster/sync/remote_stats_facade.py`. The page cannot tell the difference,
which is the point.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.cluster.sync.remote_stats_facade import make_facades
from backend.src.services.breakout_signal import adaptive_params as _real_params
from backend.src.services.breakout_signal import breakout_signal_repo as _real_db
from backend.src.services.breakout_signal import ml_engine as _real_ml

_db, _ml, _params = make_facades("breakout", _real_db, _real_ml, _real_params)

__all__ = ["virtual_balance", "max_drawdown", "stats", "open_signals", "all_signals", "perf_by_breakout_type", "perf_by_adx_band", "perf_by_session", "perf_by_bias", "analysis_log", "adaptive_params", "ml_summary", "ml_metrics", "get_config", "set_config", "reset_adaptive_params", "ml_thresholds"]

async def virtual_balance():
    return await to_db_thread(_db.get_virtual_balance)

async def max_drawdown():
    return await to_db_thread(_db.get_max_drawdown)

async def stats():
    return await to_db_thread(_db.get_stats)

async def open_signals():
    return await to_db_thread(_db.get_open_signals)

async def all_signals(limit: int = 80):
    return await to_db_thread(_db.get_all_signals, limit=limit)

async def perf_by_breakout_type():
    return await to_db_thread(_db.get_perf_by_breakout_type)

async def perf_by_adx_band():
    return await to_db_thread(_db.get_perf_by_adx_band)

async def perf_by_session():
    return await to_db_thread(_db.get_perf_by_session)

async def perf_by_bias():
    return await to_db_thread(_db.get_perf_by_bias)

async def analysis_log(limit: int = 40):
    return await to_db_thread(_db.get_analysis_log, limit=limit)

async def adaptive_params():
    return await to_db_thread(_params.get_all)

async def ml_summary():
    return await to_db_thread(_ml.summary)

async def ml_metrics():
    return await to_db_thread(_ml.get_ml_metrics)


# -- Synchronous config + ML constants ---------------------------------------
# Not dispatched off-loop: these are single-row reads or in-memory module
# constants, and the panels use them while building widgets rather than in a
# refresh tick.

def get_config(key: str, default: str = "") -> str:
    return _db.get_config(key, default)


def set_config(key: str, value: str) -> None:
    _db.set_config(key, value)


def reset_adaptive_params() -> None:
    _params.reset_to_defaults()


def ml_thresholds() -> dict:
    """MIN_TRAIN_SAMPLES / RETRAIN_EVERY, which the panel renders as prose."""
    return {"min_train_samples": _ml.MIN_TRAIN_SAMPLES,
            "retrain_every": _ml.RETRAIN_EVERY}
