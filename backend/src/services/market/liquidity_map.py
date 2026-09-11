"""The levels an institutional desk references intraday.

Section 4.1 of `docs/todo/reversal-engine/200`. `level_detector` produces the
Asia range, H1 swings, round numbers, congestion zones and the ICT unicorn.
It has no previous-day high or low, no previous-week range, no daily or
weekly open, and no initial balance -- among the most reliably reacted-to
intraday levels on gold, and every one of them computable from the D1 and
intraday candles the bridge already serves. No new feed, no new dependency.

`as_candidate_levels` emits `{price, type, strength}`, the dict shape
`level_detector.score_level` and `_deduplicate_levels` already consume, so
these slot into the existing candidate list rather than needing a parallel
path through the engine.

**The new types are new names on purpose.** Reporting a previous-day high as
a `swing_high` would silently inherit that type's fitted score and make the
new levels invisible in every per-type performance table. They carry their
own names so their edge can be measured before anyone trusts it.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Optional, Sequence

from backend.src.services.market import volume_profile as _vp
from backend.src.services.market import vwap as _vwap

LEVEL_TYPES = (
    "pdh", "pdl", "pdc", "pd_mid", "daily_open",
    "pwh", "pwl", "weekly_open",
    "ib_high", "ib_low",
    # From the same intraday candles the initial balance already needs, so
    # they cost no extra data: the session's volume-weighted average price,
    # and the busiest price plus the edges of the value area around it.
    "vwap", "poc", "vah", "val",
)

# Provisional, and deliberately below every type level_detector.score_level
# has actually measured. These have no fitted edge on this engine's own data
# yet; giving them a competitive score before that measurement exists would
# let them outrank levels whose edge IS measured. Raise them when the
# per-type attribution says to, not before.
PROVISIONAL_STRENGTH = 2


def _ohlc(candle: dict) -> tuple[float, float, float, float]:
    o = float(candle.get("open", candle.get("o", 0)) or 0)
    h = float(candle.get("high", candle.get("h", 0)) or 0)
    l = float(candle.get("low", candle.get("l", 0)) or 0)
    c = float(candle.get("close", candle.get("c", 0)) or 0)
    return o, h, l, c


def _ts(candle: dict) -> float:
    return float(candle.get("ts") or candle.get("time") or 0.0)


def _sorted(candles: Sequence[dict]) -> list[dict]:
    return sorted((c for c in candles or () if _ts(c) > 0), key=_ts)


def prior_day_levels(d1_candles: Sequence[dict],
                     now: float) -> Optional[dict]:
    """Previous day's high, low, close and midpoint, plus today's open.

    "Previous day" is the completed D1 candle before the one `now` falls in,
    taken from the broker's own daily bars rather than recomputed from
    intraday data -- the broker's day boundary is the one its chart shows and
    therefore the one other participants are looking at.
    """
    rows = _sorted(d1_candles)
    current = [c for c in rows if _ts(c) <= now]
    if len(current) < 2:
        return None
    today, prior = current[-1], current[-2]
    _o, h, l, c = _ohlc(prior)
    if h <= 0 or l <= 0:
        return None
    return {
        "pdh": h, "pdl": l, "pdc": c,
        "pd_mid": round((h + l) / 2.0, 5),
        "daily_open": _ohlc(today)[0],
    }


def _week_start(ts: float) -> float:
    """Midnight UTC on the Monday of the week `ts` falls in."""
    d = datetime.fromtimestamp(ts, tz=timezone.utc)
    monday = (d - timedelta(days=d.weekday())).replace(
        hour=0, minute=0, second=0, microsecond=0)
    return monday.timestamp()


def prior_week_levels(d1_candles: Sequence[dict],
                      now: float) -> Optional[dict]:
    """Previous calendar week's high and low, plus this week's open."""
    rows = _sorted(d1_candles)
    if not rows:
        return None
    this_start = _week_start(now)
    last_start = this_start - 7 * 86_400.0

    last = [c for c in rows if last_start <= _ts(c) < this_start]
    this = [c for c in rows if this_start <= _ts(c) <= now]
    if not last:
        return None

    highs = [_ohlc(c)[1] for c in last if _ohlc(c)[1] > 0]
    lows = [_ohlc(c)[2] for c in last if _ohlc(c)[2] > 0]
    if not highs or not lows:
        return None

    out = {"pwh": max(highs), "pwl": min(lows)}
    if this:
        out["weekly_open"] = _ohlc(this[0])[0]
    return out


def initial_balance(candles: Sequence[dict], session_open_ts: float,
                    minutes: int = 60) -> Optional[dict]:
    """The high and low of the session's first `minutes`.

    The initial balance is what the session's early participants agreed
    value was; the rest of the day is measured as an extension of it or a
    failure to leave it. One hour is the convention and the parameter is
    there because London and New York are not the same session.
    """
    end = session_open_ts + minutes * 60.0
    window = [c for c in candles or ()
              if session_open_ts <= _ts(c) < end]
    highs = [_ohlc(c)[1] for c in window if _ohlc(c)[1] > 0]
    lows = [_ohlc(c)[2] for c in window if _ohlc(c)[2] > 0]
    if not highs or not lows:
        return None
    return {"ib_high": max(highs), "ib_low": min(lows)}


def as_candidate_levels(d1_candles: Sequence[dict], now: float,
                        intraday_candles: Optional[Sequence[dict]] = None,
                        session_open_ts: Optional[float] = None,
                        minutes: int = 60) -> list[dict]:
    """Every level this module can compute, in `level_detector`'s dict shape."""
    found: dict[str, float] = {}
    for group in (prior_day_levels(d1_candles, now),
                  prior_week_levels(d1_candles, now)):
        if group:
            found.update(group)

    if intraday_candles and session_open_ts:
        ib = initial_balance(intraday_candles, session_open_ts, minutes)
        if ib:
            found.update(ib)
        found.update(session_reference_prices(intraday_candles, session_open_ts))

    return [{"price": round(float(price), 2), "type": name,
             "strength": PROVISIONAL_STRENGTH}
            for name, price in found.items()
            if name in LEVEL_TYPES and float(price) > 0]


def session_reference_prices(candles: Sequence[dict],
                             session_open_ts: float) -> dict:
    """Session VWAP, and the point of control / value area around it.

    The two most widely watched intraday reference prices on a real desk,
    and neither has ever existed in this app. VWAP is what institutional
    execution is benchmarked against, which is why price reacts around it;
    the point of control is where the session's activity concentrated, and
    price returns to it.

    Anything the data cannot support is simply absent from the result. A
    profile needs a price range, and a POC of 0.0 entering the candidate
    list would be a level at a price the market has never traded at.
    """
    out: dict[str, float] = {}
    v = _vwap.vwap(candles, anchor_ts=session_open_ts)
    if v and v.vwap > 0:
        out["vwap"] = v.vwap

    session = [c for c in candles
               if float(c.get("ts") or c.get("time") or 0.0) >= session_open_ts]
    profile = _vp.build(session)
    if profile:
        out["poc"] = profile.poc
        out["vah"] = profile.vah
        out["val"] = profile.val
    return out
