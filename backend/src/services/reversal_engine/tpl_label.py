"""Label every triggered signal with the trade the EA template places.

docs/todo/reversal-engine/240. The engine's own virtual outcome is not the
trade that goes to the broker: on losses its fill sits ~6.8 points from its
stop and TP1 ~2.1 points away, where the live template ("30 TP1 SL50 and
Trail") uses 5 and 4 with a ladder behind. A model trained on the first
predicts a trade nobody places. This replays each signal from its real
trigger price and time through the template on M1 bars
(`entry_study.template_policy`, the same replay the study checked against
211 real executed trades: sign agreed 86%, correlation 0.80) and stores the
R in `re_signals.tpl_r`.

One UTC day per pass of the research loop's minute timer, newest first,
like `xasset_sweep`. **Only candle reads.** Nothing here places, closes or
modifies a trade.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional, Sequence

from backend.src.services.reversal_engine import entry_study as es
from backend.src.services.reversal_engine import tpl_label_repo

log = logging.getLogger("reversal_engine")

# Round-trip cost in points, measured for this template in
# execution_quality on 2026-09-24: spread 0.219 plus slippage 0.357 over
# 446 trades.
COST_PTS = 0.575

# Days the broker served no bars for. Skipped for the life of the process.
_skip_days: set[str] = set()

_UNSET = object()


def label_from_bars(bars: Sequence[es.Bar], direction: str, trigger_price: float,
                    trigger_time: float) -> Optional[float]:
    """R of the template trade entered at `trigger_price` at `trigger_time`,
    or None when the bars do not cover the entry.

    The entry bar is the one the trigger fell in, walked on its adverse
    side only (`entry_study.path_after`): its favourable extreme may predate
    the fill, and its adverse extreme may too, which makes the label
    slightly pessimistic rather than flattering.
    """
    idx = None
    for i, b in enumerate(bars):
        if b.ts <= trigger_time:
            idx = i
        else:
            break
    if idx is None or trigger_time - bars[idx].ts >= es.BAR_S:
        return None
    entry = es.Entry(idx, float(trigger_price), float(trigger_time))
    res = es.replay(bars, entry, direction, es.template_policy(COST_PTS))
    return None if res is None else round(res.r_multiple, 4)


def _day(ts: float) -> str:
    return datetime.fromtimestamp(float(ts), timezone.utc).strftime("%Y-%m-%d")


def _engine_bridge() -> Optional[Any]:
    from backend.src.services.reversal_engine.study_schedule import _bridge_of_running_engine
    return _bridge_of_running_engine()


async def tpl_label_sweep(engine: Any, bridge: Any = _UNSET,
                          bridge_getter: Optional[Callable[[], Any]] = None,
                          now: Optional[float] = None) -> None:
    """One pass: label the newest day that has unlabelled triggered signals.

    A trade is labelled only once the replay horizon has passed, so the
    label is never a trade cut short by the end of the data.
    """
    if bridge is _UNSET:
        bridge = (bridge_getter or _engine_bridge)()
    if bridge is None:
        return
    now = time.time() if now is None else now

    by_day: dict[str, list[dict]] = {}
    for r in tpl_label_repo.signals_missing_tpl(now - es.HORIZON_S - 600):
        day = _day(r["trigger_time"])
        if day not in _skip_days:
            by_day.setdefault(day, []).append(r)
    if not by_day:
        return

    day = max(by_day)
    rows = by_day[day]
    lo = min(float(r["trigger_time"]) for r in rows) - 2 * es.BAR_S
    hi = max(float(r["trigger_time"]) for r in rows) + es.HORIZON_S + 2 * es.BAR_S
    bars = es.bars_from(await bridge.get_candles_range(lo, hi, "M1"))
    if not bars:
        _skip_days.add(day)
        log.info("[RE-Label] no M1 history for %s; skipped for this run", day)
        return

    pairs, unlabelled = [], 0
    for r in rows:
        lab = label_from_bars(bars, str(r["direction"]).upper(),
                              float(r["trigger_price"]), float(r["trigger_time"]))
        if lab is None:
            unlabelled += 1
            continue
        pairs.append((int(r["id"]), lab))
    tpl_label_repo.store_tpl(pairs)
    if unlabelled:
        # Bars missing around these triggers; do not ask for this day again.
        _skip_days.add(day)
    log.info("[RE-Label] labelled %d signals on %s with the template's exits "
             "(%d without bars; %d days still to do)",
             len(pairs), day, unlabelled, len(by_day) - 1)
