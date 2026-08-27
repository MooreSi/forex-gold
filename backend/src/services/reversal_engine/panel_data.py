"""Panel reads for the Reversal Engine signal-generator page.

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
from backend.src.services.reversal_engine import ml_engine as _real_ml
from backend.src.services.reversal_engine import reversal_engine_repo as _real_db
from backend.src.db import database as _core_db

_db, _ml, _ = make_facades("reversal_engine", _real_db, _real_ml)

__all__ = ["virtual_balance", "max_drawdown", "stats", "active_levels", "open_signals", "all_signals", "perf_by_session", "perf_by_bias", "perf_by_level_type", "analysis_log", "ml_summary", "ml_metrics", "ml_thresholds"]

async def virtual_balance():
    return await to_db_thread(_db.get_virtual_balance)

async def max_drawdown():
    return await to_db_thread(_db.get_max_drawdown)

async def stats():
    return await to_db_thread(_db.get_stats)

async def active_levels():
    return await to_db_thread(_db.get_active_levels)

async def open_signals():
    return await to_db_thread(_db.get_open_signals)

async def all_signals(limit: int = 80):
    return await to_db_thread(_db.get_all_signals, limit=limit)

async def perf_by_session():
    return await to_db_thread(_db.get_perf_by_session)

async def perf_by_bias():
    return await to_db_thread(_db.get_perf_by_bias)

async def perf_by_level_type():
    return await to_db_thread(_db.get_perf_by_level_type)

async def analysis_log(limit: int = 30):
    return await to_db_thread(_db.get_analysis_log, limit=limit)

async def ml_summary():
    return await to_db_thread(_ml.summary)

async def ml_metrics():
    return await to_db_thread(_ml.get_ml_metrics)


# -- ML constants -------------------------------------------------------------

def ml_thresholds() -> dict:
    """MIN_TRAIN_SAMPLES / RETRAIN_EVERY, which the panel renders as prose."""
    return {"min_train_samples": _ml.MIN_TRAIN_SAMPLES,
            "retrain_every": _ml.RETRAIN_EVERY}


async def get_realised_pnl() -> dict:
    """What this engine's trades actually made, from the core trade ledger.

    Async and offloaded like every other accessor in this module: the
    controller layer must not import the database (a contract enforced at
    zero), so the thread hop belongs here rather than at the call site.

    Deliberately not read from reversal_engine.db: that database records
    every signal the generator produced and prices them all at the virtual
    lot, whether or not the trade was ever placed. Only rows here in
    vantage_simulated_trades correspond to orders that really went to MT5.
    """
    from backend.src.services.analytics import read_repo as _reads
    return await to_db_thread(_reads.realised_pnl_for_source, "Reversal Engine")
