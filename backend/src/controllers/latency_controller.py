"""Settings > Latency (docs/todo/006).

Two operations, both forwarded to `services/diagnostics/latency`: the passive
read (per-hop numbers from real signals) and the check (live probes plus the
VPS). Neither places, modifies or closes an order.
"""
from __future__ import annotations

from typing import Any

from backend.src.services.diagnostics import latency as _latency

__all__ = ["snapshot", "check"]


def snapshot() -> dict:
    """Both checkers' hops from the signals seen since start, and the polling waits."""
    return _latency.passive_view()


async def check(engine: Any, reader: Any) -> dict:
    """Run every live probe here and, when paired, on the VPS."""
    return await _latency.check(engine, reader)
