"""How far each closed trade went against us and in our favour (docs/todo/024).

MAE (maximum adverse excursion) and MFE (maximum favourable excursion), in
price, for every position in the Analysis table. Until now only the Reversal
Engine's research measured them (`reversal_engine/excursion_backfill.py`, from
ticks), so signal, manual and template trades had none.

**From candles, not ticks.** Tick-history reads hang on the broker's current
access point and the client pauses them for hours at a time (5b08eeb9,
a105320a). One position read of M1 bars covers every trade inside it; trades
older than that read are measured on M5; older than both are remembered as
unmeasurable.

**One clock.** `engine.get_candles` returns bars stamped in MT5 server time
(`copy_rates_from_pos`, no offset applied by the bridge), which is also what a
deal's `time` is. They are compared directly. `formatting.BROKER_OFFSET` is a
fixed 3 h and the broker runs on 2 h in winter, so converting either side
would put a trade on the wrong hour for half the year. `get_candles_range` is
not used for the same reason, and because it is not on the runtime facade.

**The edge bars count only the trade's own prices.** The bar the trade opened
in also holds price from before the fill, and the bar it closed in price from
after the exit. Their high and low are not used: the entry and exit prices
stand in for them, so no figure is inflated by price the trade did not hold.
They are used once, as a check that the bars and the deal agree: an entry or
exit price its bar never traded (beyond `_ASK_ALLOWANCE`) means the two are on
different clocks or the history is wrong, and the trade is recorded as
unmeasurable rather than measured on the wrong minutes.

Bars are bid prices. A buy is closed at the bid, so a long's path is exact; a
short is closed at the ask, so a short's adverse excursion is understated by
the spread. The resolution is stored and shown so the reader can judge.

**A hole in the history is not a verdict.** On the demo account on
2026-10-20, 11 of 44 trades fell where the terminal held no M1 bars at all
(the bridge was torn down 46 times on 2026-10-19 and froze again that
morning). MT5 may backfill such a hole, and a hole in the MIDDLE of a trade
would silently understate both figures. `covered` therefore requires the entry
and exit bars, and no run of more than `_HOLE_BARS` missing bars inside one
server day (the daily break and the weekend both cross server midnight). A
trade that fails it is not measured and not recorded; it is remembered for the
life of the process, like `entry_spread`'s misses, and tried again after a
restart.

Bounded like `entry_spread`: at most `PER_READ` positions per table read,
newest first, the rest on later polls. A bridge error or an empty read records
nothing, so the next poll tries again. Nothing here places, closes or modifies
anything.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from backend.src.services.broker import candle_history as _history
from backend.src.services.positions import excursion_cache as _cache

log = logging.getLogger(__name__)

__all__ = ["fill", "row_fields", "cached"]

PER_READ = 60
MAX_BARS = _history.MAX_HISTORY_BARS
_TF_SECONDS = {"M1": 60, "M5": 300}
# MT5 server time runs up to 3 h ahead of this machine's UTC clock, and a bar
# count estimated from UTC would fall short by that much.
_CLOCK_SLACK_S = 4 * 3600
# A buy fills at the ask, above the bid bars. Gold's spread is normally well
# under this; an hour's wrong clock is normally far over it.
_ASK_ALLOWANCE = 0.60
# XAUUSD: one pip is a $0.10 price move.
_PIPS_PER_PRICE = 10.0
# A quiet spell is a few missing minutes; more than this inside one server day
# is a hole in the terminal's history.
_HOLE_BARS = 5
_DAY_S = 86_400

_gaps: set[int] = set()

# (ticket, direction, entry, exit, open_ts, close_ts) -- broker time.
Need = tuple[int, str, float, float, float, float]


def _bar_at(bars: list[dict], ts: float, bar_s: int) -> Optional[dict]:
    for b in bars:
        start = float(b.get("ts") or 0)
        if start <= ts < start + bar_s:
            return b
    return None


def _traded(bar: dict, price: float) -> bool:
    return (float(bar["low"]) - _ASK_ALLOWANCE) <= price <= (float(bar["high"]) + _ASK_ALLOWANCE)


def covered(bars: list[dict], open_ts: float, close_ts: float, bar_s: int) -> bool:
    """Whether `bars` hold the whole of the trade's path."""
    first = _bar_at(bars, open_ts, bar_s)
    last = _bar_at(bars, close_ts, bar_s)
    if first is None or last is None:
        return False
    lo, hi = float(first["ts"]), float(last["ts"])
    span = sorted(float(b["ts"]) for b in bars if lo <= float(b.get("ts") or 0) <= hi)
    for a, b in zip(span, span[1:]):
        if b - a > _HOLE_BARS * bar_s and int(a // _DAY_S) == int(b // _DAY_S):
            return False
    return True


def measure(direction: str, entry: float, exit_price: float, open_ts: float,
            close_ts: float, bars: list[dict], bar_s: int) -> Optional[dict]:
    """`{mae, mfe}` in price, both >= 0, or None when it cannot be told."""
    first = _bar_at(bars, open_ts, bar_s)
    last = _bar_at(bars, close_ts, bar_s)
    if first is None or last is None:
        return None
    if not (_traded(first, entry) and _traded(last, exit_price)):
        return None
    first_ts, last_ts = float(first["ts"]), float(last["ts"])
    highs = [entry, exit_price]
    lows = [entry, exit_price]
    for b in bars:
        ts = float(b.get("ts") or 0)
        if first_ts < ts < last_ts:
            highs.append(float(b["high"]))
            lows.append(float(b["low"]))
    hi, lo = max(highs), min(lows)
    if str(direction).upper() == "BUY":
        return {"mae": round(entry - lo, 5), "mfe": round(hi - entry, 5)}
    return {"mae": round(hi - entry, 5), "mfe": round(entry - lo, 5)}


async def _read(engine: Any, tf: str, oldest_open: float) -> list[dict]:
    """Bars reaching back to `oldest_open`, oldest first. [] on any failure."""
    bar_s = _TF_SECONDS[tf]
    want = int((time.time() + _CLOCK_SLACK_S - oldest_open) // bar_s) + _history.SLACK_BARS
    count = max(10, min(want, MAX_BARS))
    try:
        bars = await engine.get_candles(tf, count)
    except Exception as exc:
        log.debug("[trade_excursion] %s read failed: %s", tf, exc)
        return []
    return sorted((b for b in bars or [] if b.get("ts")), key=lambda b: float(b["ts"]))


async def _record(found: dict, ticket: int, got: Optional[dict], res: str) -> None:
    value = ({"mae": got["mae"], "mfe": got["mfe"], "res": res} if got
             else {"mae": None, "mfe": None, "res": "none"})
    await _cache.cache(ticket, value["mae"], value["mfe"], value["res"])
    found[ticket] = value


async def fill(engine: Any, needs: list[Need]) -> dict[int, dict]:
    """Measure and cache the positions in `needs`, newest first.

    Returns what it recorded, keyed by ticket. Never raises.
    """
    if not hasattr(engine, "get_candles"):
        return {}
    todo = sorted((n for n in needs
                   if n[0] not in _gaps and n[2] and n[4] and n[5] and n[5] >= n[4]),
                  key=lambda n: n[5], reverse=True)[:PER_READ]
    found: dict[int, dict] = {}
    try:
        for tf in ("M1", "M5"):
            if not todo:
                break
            bars = await _read(engine, tf, min(n[4] for n in todo))
            if not bars:
                # Bridge down or no history: not a verdict on any trade.
                return found
            oldest = float(bars[0]["ts"])
            still = []
            for need in todo:
                ticket, direction, entry, exit_price, open_ts, close_ts = need
                if open_ts < oldest:
                    still.append(need)
                    continue
                if not covered(bars, open_ts, close_ts, _TF_SECONDS[tf]):
                    _gaps.add(ticket)
                    continue
                got = measure(direction, entry, exit_price, open_ts, close_ts,
                              bars, _TF_SECONDS[tf])
                await _record(found, ticket, got, tf)
            todo = still
        # Older than the widest read reaches: it will never be measurable.
        for need in todo:
            await _record(found, need[0], None, "none")
    except Exception as exc:
        log.debug("[trade_excursion] fill stopped: %s", exc)
    return found


async def cached(tickets: list[int]) -> dict[int, dict]:
    """What the cache already holds. {} on any error."""
    try:
        return await _cache.get_cached(tickets)
    except Exception:
        return {}


def row_fields(ex: Optional[dict], pips: Optional[float], lots: float,
               contract_size: float) -> dict:
    """The table row's excursion columns.

    $ is at the OPENING lots, so for a position closed in parts it is the
    most the excursion could have been worth, not what it was.
    `capture` is the share of the best price reached that the trade banked.
    """
    blank = {"mae_pips": None, "mfe_pips": None, "mae_usd": None,
             "mfe_usd": None, "capture": None, "excursion_res": None}
    if not ex:
        return blank
    if ex.get("mae") is None or ex.get("mfe") is None:
        return {**blank, "excursion_res": ex.get("res")}
    mae, mfe = float(ex["mae"]), float(ex["mfe"])
    mfe_pips = mfe * _PIPS_PER_PRICE
    return {
        "mae_pips": round(mae * _PIPS_PER_PRICE, 1),
        "mfe_pips": round(mfe_pips, 1),
        "mae_usd": round(mae * lots * contract_size, 2),
        "mfe_usd": round(mfe * lots * contract_size, 2),
        "capture": round(pips / mfe_pips, 3) if pips is not None and mfe_pips > 0 else None,
        "excursion_res": ex.get("res"),
    }
