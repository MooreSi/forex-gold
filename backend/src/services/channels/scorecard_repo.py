"""Read-only channel_performance queries for the scorecard UI.

Lifted out of repo.py 2026-09-03: that file sits against its 800-line ceiling,
and the Telegram-Auto consolidation needed a few lines inside it. A display
accessor is the least load-bearing thing in there, so it moved rather than the
knowledge in repo.py's comments being trimmed away to make room.

Nothing here writes.
"""
from __future__ import annotations

from backend.src.db.database import db

from .repo import _canonical


def get_channel_performance_map() -> dict:
    """{source: {lot_mult, paused, manual_override}} for the scorecard UI,
    keyed canonically so a pre-migration wrapper row folds onto its channel."""
    try:
        with db() as conn:
            rows = conn.execute(
                "SELECT source, lot_mult, paused, manual_override FROM channel_performance"
            ).fetchall()
        out: dict = {}
        for r in sorted(rows, key=lambda x: _canonical(x[0]) != x[0]):
            out.setdefault(_canonical(r[0]), {
                "lot_mult": float(r[1] or 1.0), "paused": bool(r[2]),
                "manual_override": bool(r[3])})
        return out
    except Exception:
        return {}
