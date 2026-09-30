"""Everything the Trend PA panel shows, in one read, from the right node.

`local_report()` is this node's own numbers; it is also exactly what the VPS
mirrors to the Mac in the `signal_gen_stats` broadcast under "trend_pa". So
`report()` in Remote mode returns the VPS's copy through the same facade the
other engines' panels use, and the panel cannot tell the difference except
by `where`, which it shows.

Live and backtest are summarised separately and never pooled: a replay is
evidence about the past, a live number is evidence about now.
"""
from __future__ import annotations

import time

from backend.src.db.database import to_db_thread
from backend.src.services.cluster.sync import remote_stats_facade as _facade
from backend.src.services.trend_pa import ml
from backend.src.services.trend_pa import repo
from backend.src.services.trend_pa import service
from backend.src.services.trend_pa import stats as ss
from backend.src.services.trend_pa import strategy as st

__all__ = ["local_report", "report", "request_backtest"]

_LIST = 25


def _slim(rows: list) -> list:
    """Signals without their feature vectors: the panel does not draw them
    and they are most of each row's size on the sync wire."""
    return [{k: v for k, v in r.items() if k != "features"} for r in rows]


def local_report() -> dict:
    eng = service.get_instance()
    rr = st.DEFAULTS["rr"]
    model_state = dict(eng.model.state) if eng is not None else {}
    backtest_at = repo.get_config("backtest_at")
    return {
        "running": bool(getattr(eng, "is_running", False)),
        "status": getattr(eng, "status_detail", "") if eng is not None else "",
        "generating_here": getattr(eng, "generating_here", None),
        "last_cycle_at": getattr(eng, "last_cycle_at", None),
        "last_evaluated_at": getattr(eng, "last_evaluated_at", None),
        "live": ss.summarize(repo.closed_signals(origin="live"), rr=rr),
        "backtest": ss.summarize(repo.closed_signals(origin="backtest"), rr=rr),
        "backtest_at": float(backtest_at) if backtest_at else None,
        "backtest_running": bool(getattr(eng, "backtest_running", False)),
        "open": _slim(repo.open_signals()),
        "recent": _slim(repo.recent_signals(_LIST)),
        "log": repo.analysis_log(_LIST),
        "ml": {**model_state, "min_samples": ml.MIN_SAMPLES, "min_auc": ml.MIN_AUC},
        "rules": {k: st.DEFAULTS[k] for k in (
            "rr", "session_start_utc", "session_end_utc", "friday_cutoff_utc",
            "ema_period", "min_sl_atr", "max_sl_atr")},
        "generated_at": time.time(),
    }


async def report() -> dict:
    if _facade._is_remote_active():
        return {**_facade._remote_engine_stats("trend_pa"), "where": "remote"}
    return {**await to_db_thread(local_report), "where": "local"}


async def request_backtest() -> dict:
    """Run the replay on the node the panel is showing."""
    from backend.src.services.cluster import remote_control as _remote
    if _remote.is_remote_active():
        await _remote.send_engine_action("trend_pa", "backtest")
        return {"started": True, "where": "remote"}
    eng = service.get_instance()
    if eng is None:
        return {"started": False, "where": "local", "error": "the engine is not built here"}
    if eng.backtest_running:
        return {"started": False, "where": "local", "error": "a backtest is already running"}
    import asyncio
    asyncio.ensure_future(eng.run_backtest())
    return {"started": True, "where": "local"}
