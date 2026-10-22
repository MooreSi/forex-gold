"""Splitting rows into frames the sync link accepts.

The VPS's sync server takes websockets' default 1 MiB frame, and a frame over
it closes the link (1009); that is how the consolidated ledger made the Mac
reconnect for ever on 2026-10-18. Rows go in chunks of at most `CHUNK_BYTES`.
"""
from __future__ import annotations

import json

from backend.src.services.cluster.sync.protocol import LEDGER_PUSH_CHUNK_BYTES

CHUNK_BYTES = LEDGER_PUSH_CHUNK_BYTES


def chunk_rows(rows: list[dict], max_bytes: int = CHUNK_BYTES) -> list[list[dict]]:
    """Consecutive chunks whose JSON list fits in `max_bytes`. A row that is
    bigger on its own travels alone rather than being dropped."""
    chunks: list[list[dict]] = []
    current: list[dict] = []
    size = 2  # the list's brackets
    for row in rows:
        n = len(json.dumps(row, default=str)) + 2  # ", " between items
        if current and size + n > max_bytes:
            chunks.append(current)
            current, size = [], 2
        current.append(row)
        size += n
    if current:
        chunks.append(current)
    return chunks
