"""Measured fill cost for the Dashboard. Read-only. Forwards to
backend.src.services.broker.fill_cost_report unchanged."""
from __future__ import annotations

from backend.src.services.broker import fill_cost_report as _report

__all__ = ["report_async"]


async def report_async(*args, **kwargs):
    return await _report.report_async(*args, **kwargs)
