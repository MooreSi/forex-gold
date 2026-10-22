"""Merging ML training data with the paired VPS (docs/todo/026).

Starts the merge on this node's sync link and reads back its progress and
each dataset's verdict. Nothing here places, closes or sizes a trade.
"""
from __future__ import annotations

from backend.src.services.ml_merge import service as _merge

__all__ = ["start_merge", "merge_status", "MergeFailed"]

MergeFailed = _merge.MergeFailed


async def start_merge() -> None:
    return await _merge.start_on_link()


async def merge_status() -> dict:
    return await _merge.status_async()
