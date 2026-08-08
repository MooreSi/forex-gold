"""DPM Analysis page reads.

Each runs on the DB worker thread because the page calls these from ui.timer
callbacks. Failures collapse to [] exactly as the page's own fetchers always
did -- an empty table is the intended degraded state for a panel whose tables
may legitimately not exist yet on a fresh install.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.dpm import repo as _repo

__all__ = ["performance_rows", "calibration_rows", "calibration_runs"]


async def performance_rows() -> list[dict]:
    try:
        return await to_db_thread(_repo.fetch_performance_with_trades)
    except Exception:
        return []


async def calibration_rows() -> list[dict]:
    try:
        return await to_db_thread(_repo.fetch_latest_calibration_full)
    except Exception:
        return []


async def calibration_runs() -> list[dict]:
    try:
        return await to_db_thread(_repo.fetch_calibration_run_summaries)
    except Exception:
        return []
