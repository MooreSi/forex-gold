"""Engine panels' API (M3 page drains) -- shared by the Breakout, Reversal
and Bounce (test_signal) panels.

The panels' DB use is one pattern: dispatch their own engine-repo reads to
the DB worker thread (their refresh timers run on the event loop, and a
synchronous read there stalls the whole app), plus a couple of
risk-settings reads for the live-execution badges. run_db is that dispatch,
by the same name the pages can keep reading naturally.
"""
from __future__ import annotations

from backend.src.db import database as db_module


async def run_db(fn, *args, **kwargs):
    """Run a synchronous repo/engine read on the dedicated DB worker thread."""
    return await db_module.to_db_thread(fn, *args, **kwargs)


def get_risk_settings() -> dict:
    return db_module.get_risk_settings()


async def get_risk_settings_async() -> dict:
    return await db_module.to_db_thread(db_module.get_risk_settings)


def update_risk_settings(fields: dict) -> None:
    db_module.update_risk_settings(fields)
