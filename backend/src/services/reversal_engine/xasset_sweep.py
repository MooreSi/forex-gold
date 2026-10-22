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

# Passes in a row with gold and no peer at all before a day is left for this
# run. One miss is the bridge and is retried; a broker that names every peer
# differently misses forever, and was asked every minute (docs/todo/broker/010).
PEER_MISSES_BEFORE_SKIP = 5
_peer_misses: dict[str, int] = {}

# Days measured again this run. A day whose peers were stored missing is read
# once more per run, not every minute: some peers are genuinely shut on some
# days, and those gain nothing from asking again.
_remeasured_days: set[str] = set()

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
    """One pass: measure the newest unmeasured day, if any; else measure
    again the newest day whose peers were stored missing."""
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
        await _remeasure(bridge)
        return

    day = max(by_day)
    rows = by_day[day]
    candles = await _read_day(bridge, day, rows)
    if candles is None:
        return
    gold, peers = candles

    pairs = []
    for r in rows:
        snap = xa.snapshot(gold, peers, float(r["created_at"]), r.get("direction") or "")
        pairs.append((int(r["id"]), json.dumps(snap)))
    xasset_repo.store_xasset(pairs)
    log.info("[RE-XAsset] measured %d signals on %s (%d days still to do)",
             len(pairs), day, len(by_day) - 1)


async def _read_day(bridge: Any, day: str, rows: list[dict]) -> Optional[tuple]:
    """(gold, {peer: bars}) covering the day's signals, or None when the day
    cannot be measured this pass."""
    t_min = min(float(r["created_at"]) for r in rows)
    t_max = max(float(r["created_at"]) for r in rows)
    lo, hi = t_min - _LEAD_S, t_max

    gold = await bridge.get_candles_range_for_symbol(xa.GOLD, lo, hi, "M5")
    if not gold:
        _skip_days.add(day)
        log.info("[RE-XAsset] no gold history for %s; skipped for this run", day)
        return None

    peers = {}
    for p in xa.PEERS:
        peers[p] = await bridge.get_candles_range_for_symbol(p, lo, hi, "M5")
    if not any(peers.values()):
        # Gold came back and nothing else did: the bridge, not the market.
        misses = _peer_misses[day] = _peer_misses.get(day, 0) + 1
        if misses >= PEER_MISSES_BEFORE_SKIP:
            _skip_days.add(day)
            del _peer_misses[day]
            log.info("[RE-XAsset] no peer served for %s %d times; skipped for this run",
                     day, misses)
        else:
            log.info("[RE-XAsset] no peer served for %s; will retry", day)
        return None
    _peer_misses.pop(day, None)
    return gold, peers


async def _remeasure(bridge: Any) -> None:
    """Measure again the newest day on which some peer is missing from every
    signal: a feed that did not serve it, not a market shut for part of the
    day. Until 2026-10-22 the bridge read six of the seven peers from MT5's
    stale cache, and those signals were stored once with the peers missing.
    A row is replaced only by a reading that measured more peers."""
    by_day: dict[str, list[dict]] = {}
    for r in xasset_repo.measured_signals():
        day = _day(r["created_at"])
        if day not in _skip_days and day not in _remeasured_days:
            by_day.setdefault(day, []).append(r)
    days = [d for d in sorted(by_day, reverse=True)
            if set.intersection(*(xa.missing_peers(r["xasset_json"]) for r in by_day[d]))]
    if not days:
        return

    day = days[0]
    rows = by_day[day]
    candles = await _read_day(bridge, day, rows)
    if candles is None:
        return
    _remeasured_days.add(day)
    gold, peers = candles

    pairs = []
    for r in rows:
        snap = xa.snapshot(gold, peers, float(r["created_at"]), r.get("direction") or "")
        if len(xa.missing_peers(snap)) < len(xa.missing_peers(r["xasset_json"])):
            pairs.append((int(r["id"]), json.dumps(snap)))
    xasset_repo.store_xasset(pairs)
    log.info("[RE-XAsset] measured %d of %d signals on %s again (%d days still to do)",
             len(pairs), len(rows), day, len(days) - 1)
