"""The latest GEX snapshot, for the Dashboard's GEX card (docs/todo/009).

Read-only, and display only: no signal, gate or order reads this. The card
exists so the owner can watch the history build and see the levels, not so
anything can act on them before the study in 009 says they help.

A missing figure is None, never 0. The regime comes from the sign of the
stored total GEX and nothing else; an old snapshot is flagged as old.
"""
from __future__ import annotations

import json
import time
from typing import Optional

from backend.src.services.market import gex_repo

# ~3 months of weekday snapshots: the history 009 waits for before a study.
TARGET_SNAPSHOTS = 63
# Weekday evenings only: Friday's snapshot is ~3 days old by Monday evening.
STALE_AFTER_DAYS = 4.0

_LEVELS = ("asof_date", "taken_at", "underlying", "spot", "xau_spot", "ratio",
           "total_gex", "flip_level", "call_wall", "put_wall", "xau_flip_level",
           "xau_call_wall", "xau_put_wall", "n_rows", "source", "assumptions")


def _expiries(raw) -> list[str]:
    try:
        out = json.loads(raw) if isinstance(raw, str) else raw
    except ValueError:
        return []
    return [str(e) for e in out] if isinstance(out, list) else []


def summarise(snap: Optional[dict], n_snapshots: int, now: float) -> dict:
    out = {"snapshot": None, "n_snapshots": n_snapshots, "target_snapshots": TARGET_SNAPSHOTS,
           "age_days": None, "stale": None, "regime": None, "spot_vs_flip": None}
    if not snap:
        return out
    s = {k: snap.get(k) for k in _LEVELS}
    s["expiries"] = _expiries(snap.get("expiries"))
    out["snapshot"] = s
    if s["taken_at"]:
        age = (now - float(s["taken_at"])) / 86400
        out["age_days"] = round(age, 2)
        out["stale"] = age > STALE_AFTER_DAYS
    g = s["total_gex"]
    if g:
        out["regime"] = "negative" if g < 0 else "positive"
    if s["flip_level"] is not None and s["spot"] is not None:
        out["spot_vs_flip"] = "above" if s["spot"] >= s["flip_level"] else "below"
    return out


def report() -> dict:
    return summarise(gex_repo.latest(), gex_repo.count(), time.time())


async def report_async() -> dict:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(report)
