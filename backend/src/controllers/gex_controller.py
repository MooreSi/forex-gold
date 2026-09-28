"""The latest GEX snapshot for the Dashboard. Read-only. Forwards to
backend.src.services.market.gex_report unchanged."""
from __future__ import annotations

from backend.src.services.market import gex_report as _report

__all__ = ["report_async"]


async def report_async(*args, **kwargs):
    return await _report.report_async(*args, **kwargs)
