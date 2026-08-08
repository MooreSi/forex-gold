"""Panel reads for the Bounce signal-generator page.

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
from backend.src.services.test_signal import adaptive_params as _real_params
from backend.src.services.test_signal import ml_engine as _real_ml
from backend.src.services.test_signal import test_signal_repo as _real_db

_db, _ml, _params = make_facades("bounce", _real_db, _real_ml, _real_params)

__all__ = ["virtual_balance", "max_drawdown", "stats", "consecutive_losses", "open_signals", "all_signals", "perf_by_session", "perf_by_bias", "perf_by_level_type", "analysis_log", "adaptive_params", "regime_overrides", "ml_summary", "ml_metrics", "change_signature", "get_config", "set_config", "param_specs", "perf_by_regime", "ml_features_for_signal", "ood_distance", "ml_thresholds"]

async def virtual_balance():
    return await to_db_thread(_db.get_virtual_balance)

async def max_drawdown():
    return await to_db_thread(_db.get_max_drawdown)

async def stats():
    return await to_db_thread(_db.get_stats)

async def consecutive_losses():
    return await to_db_thread(_db.get_consecutive_losses)

async def open_signals():
    return await to_db_thread(_db.get_open_signals)

async def all_signals(limit: int = 100):
    return await to_db_thread(_db.get_all_signals, limit=limit)

async def perf_by_session():
    return await to_db_thread(_db.get_perf_by_session)

async def perf_by_bias():
    return await to_db_thread(_db.get_perf_by_bias)

async def perf_by_level_type():
    return await to_db_thread(_db.get_perf_by_level_type)

async def analysis_log(limit: int = 60):
    return await to_db_thread(_db.get_analysis_log, limit=limit)

async def adaptive_params():
    return await to_db_thread(_params.get_all)

async def regime_overrides():
    return await to_db_thread(_params.get_regime_overrides)

async def ml_summary():
    return await to_db_thread(_ml.summary)

async def ml_metrics():
    return await to_db_thread(_ml.get_ml_metrics)


def _change_signature() -> tuple:
    """A cheap comparable snapshot used to decide whether the panel needs a
    re-render. Built in one worker-thread hop rather than three, because the
    30s tick runs it unconditionally -- it IS the diffing check.
    """
    stats   = _db.get_stats()
    balance = round(_db.get_virtual_balance(), 2)
    opens   = tuple(
        (s.get("id"), s.get("status"), s.get("stop_loss"),
         s.get("sl_moved_to_be"), s.get("ml_prob"))
        for s in _db.get_open_signals()
    )
    return (tuple(sorted(stats.items())), balance, opens)


async def change_signature() -> tuple:
    return await to_db_thread(_change_signature)


# -- Synchronous config, params and per-signal ML ------------------------------

def get_config(key: str, default: str = "") -> str:
    return _db.get_config(key, default)


def set_config(key: str, value: str) -> None:
    _db.set_config(key, value)


def param_specs():
    """The adaptive-params catalogue the panel renders a row per."""
    return _params.PARAMS


def perf_by_regime():
    return _db.get_perf_by_regime()


def ml_features_for_signal(signal_id):
    """Per-signal features have no mirrored remote equivalent -- always local."""
    return _db.get_ml_features_for_signal(signal_id)


def ood_distance(*args, **kwargs):
    return _ml.ood_distance(*args, **kwargs)


def ml_thresholds() -> dict:
    return {"min_train_samples": _ml.MIN_TRAIN_SAMPLES,
            "retrain_every": _ml.RETRAIN_EVERY}
