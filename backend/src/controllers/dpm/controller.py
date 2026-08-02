"""DPM Analysis page's API (M3 page drain): plain dicts in, plain dicts out.

Each read runs on the DB worker thread (to_db_thread) because the page calls
these from ui.timer callbacks -- synchronous DB work there stalls the whole
event loop (the 400-600ms VPS stalls that to_db_thread exists to prevent).
Failures collapse to [] exactly as the page's own fetchers always did.
"""
from __future__ import annotations

from backend.src.db.database import to_db_thread
from backend.src.services.dpm import repo as dpm_repo


async def get_perf_rows() -> list[dict]:
    try:
        return await to_db_thread(dpm_repo.fetch_performance_with_trades)
    except Exception:
        return []


async def get_calibration_rows() -> list[dict]:
    try:
        return await to_db_thread(dpm_repo.fetch_latest_calibration_full)
    except Exception:
        return []


async def get_calibration_runs() -> list[dict]:
    try:
        return await to_db_thread(dpm_repo.fetch_calibration_run_summaries)
    except Exception:
        return []
