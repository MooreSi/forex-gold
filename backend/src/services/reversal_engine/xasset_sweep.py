"""Record cross-asset context for every signal. docs/todo/reversal-engine/230.

One UTC day per pass of the research loop's minute timer: the newest day
with unmeasured signals first -- so a fresh signal is measured within about
a minute -- then back through history. About 60 passes cover the history
from 2026-07-23; each pass is eight candle reads (gold plus seven peers).

**Only candle reads.** Nothing here places, closes or modifies a trade.

The bridge is the running engine's, found the same way the nightly study
finds it. No node-role check, for the reason `meta_label_schedule` gives:
the timer's one `is_remote_node` call belongs to the research sweep and is
counted by `tests/core/test_reversal_research_characterization.py`, and a
remote node's engine has no bridge of its own to read through.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from backend.src.services.reversal_engine import cross_asset as xa
from backend.src.services.reversal_engine import xasset_repo

log = logging.getLogger("reversal_engine")

# Days the broker had no gold history for. Skipped for the life of the
# process rather than re-requested every minute.
_skip_days: set[str] = set()

# Enough history before the earliest signal of the day for the six-hour
# window plus the staleness allowance.
_LEAD_S = (xa.WINDOW + 2) * xa.BAR_S + xa.MAX_AGE_S

_UNSET = object()


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


def _engine_bridge() -> Optional[Any]:
    from backend.src.services.reversal_engine.study_schedule import _bridge_of_running_engine
    return _bridge_of_running_engine()


async def xasset_sweep(engine: Any, bridge: Any = _UNSET,
                       bridge_getter: Optional[Callable[[], Any]] = None) -> None:
    """One pass: measure the newest unmeasured day, if any."""
    if bridge is _UNSET:
        bridge = (bridge_getter or _engine_bridge)()
    if bridge is None:
        return

    missing = xasset_repo.signals_missing_xasset()
    by_day: dict[str, list[dict]] = {}
    for r in missing:
        day = _day(r["created_at"])
        if day not in _skip_days:
            by_day.setdefault(day, []).append(r)
    if not by_day:
        return

    day = max(by_day)
    rows = by_day[day]
    t_min = min(float(r["created_at"]) for r in rows)
    t_max = max(float(r["created_at"]) for r in rows)
    lo, hi = t_min - _LEAD_S, t_max

    gold = await bridge.get_candles_range_for_symbol(xa.GOLD, lo, hi, "M5")
    if not gold:
        _skip_days.add(day)
        log.info("[RE-XAsset] no gold history for %s; skipped for this run", day)
        return

    peers = {}
    for p in xa.PEERS:
        peers[p] = await bridge.get_candles_range_for_symbol(p, lo, hi, "M5")
    if not any(peers.values()):
        # Gold came back and nothing else did: the bridge, not the market.
        log.info("[RE-XAsset] no peer served for %s; will retry", day)
        return

    pairs = []
    for r in rows:
        snap = xa.snapshot(gold, peers, float(r["created_at"]), r.get("direction") or "")
        pairs.append((int(r["id"]), json.dumps(snap)))
    xasset_repo.store_xasset(pairs)
    log.info("[RE-XAsset] measured %d signals on %s (%d days still to do)",
             len(pairs), day, len(by_day) - 1)
