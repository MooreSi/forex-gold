"""DPM Analysis page's API: plain dicts in, plain dicts out."""
from __future__ import annotations

from backend.src.services.dpm import performance as _perf

__all__ = ["get_perf_rows", "get_calibration_rows", "get_calibration_runs"]


async def get_perf_rows() -> list[dict]:
    return await _perf.performance_rows()


async def get_calibration_rows() -> list[dict]:
    return await _perf.calibration_rows()


async def get_calibration_runs() -> list[dict]:
    return await _perf.calibration_runs()
