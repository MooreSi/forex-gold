"""Trend PA: the setup, as pure functions of closed candles.

The owner's four steps (docs/todo/012):

  1. Trend on H4 -- higher highs AND higher lows buy, lower highs AND lower
     lows sell. Research addition: the H4 close must also be on the trend's
     side of the H4 EMA50, which is the filter most published gold
     trend-pullback systems add to the bare swing count.
  2. Only in London (08-16 GMT) and New York (13-21 GMT); nothing late on a
     Friday.
  3. Wait for price to come BACK to support (up) or resistance (down): an H1
     swing level, including a broken swing high now acting as a floor. Never
     chase a big candle.
  4. Enter on a closed M15 engulfing or pin bar in the trend's direction; stop
     beyond the pullback's extreme; target 2R.

Everything here takes CLOSED candles only. The live caller drops the forming
bar; the backtest only ever hands over bars that closed before the moment it
is replaying. A function that saw a forming bar would be reading the future
in the backtest and a half-made candle live.

Candles are dicts with ts/open/high/low/close. `ts` is broker time as the
bridge sends it; `now_utc` is passed separately so nothing here needs to
know the broker's offset.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Union

DEFAULTS: dict = {
    "pivot_n": 2,              # bars each side that make a swing point
    "ema_period": 50,          # H4 EMA the close must be beyond
    "require_ema": True,
    "atr_period": 14,
    "level_lookback_h1": 120,  # five trading days of H1 swing levels
    "level_tol_atr": 0.35,     # how close a touch must come to the level
    "max_pierce_atr": 1.0,     # through the level by more than this is a break, not a test
    "min_pullback_atr": 1.0,   # price must have come back at least this far
    "pullback_lookback": 12,   # M15 bars in which to find where it came back from
    "max_candle_atr": 2.0,     # the confirmation candle may not be a breakout candle
    "max_chase_atr": 0.8,      # close no further than this beyond the level
    "pin_wick_frac": 0.6,      # rejection wick as a share of the range
    "pin_wick_body": 2.0,      # ... and as a multiple of the body
    "sl_lookback": 6,          # M15 bars whose extreme the stop goes beyond
    "sl_buffer_atr": 0.1,
    "min_sl_atr": 0.5,
    "max_sl_atr": 3.0,
    "rr": 2.0,
    "session_start_utc": 8,    # London open
    "session_end_utc": 21,     # New York close
    "friday_cutoff_utc": 19,   # before the weekend close
}


@dataclass
class Setup:
    direction: str             # BUY / SELL
    pattern: str               # engulfing / pin
    entry: float
    stop_loss: float
    take_profit: float
    risk: float
    level: float
    level_kind: str            # swing_low / swing_high (role reversal)
    session: str
    atr_m15: float
    atr_h4: float
    features: dict = field(default_factory=dict)


# ── primitives ───────────────────────────────────────────────────────────────

def atr(candles: list, period: int = 14) -> float:
    """Wilder-free simple ATR of the last `period` true ranges."""
    if len(candles) < 2:
        return 0.0
    trs = []
    for prev, cur in zip(candles[:-1], candles[1:]):
        h, l, pc = float(cur["high"]), float(cur["low"]), float(prev["close"])
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    tail = trs[-period:]
    return sum(tail) / len(tail) if tail else 0.0


def ema(values: list, period: int) -> Optional[float]:
    """Last EMA value, seeded with the SMA of the first `period`. None when
    there is not enough history -- a guessed EMA is how the breakout engine's
    H4 bias was the starvation fallback on 122 of 122 signals (bugs/060)."""
    if len(values) < period:
        return None
    k = 2.0 / (period + 1)
    e = sum(values[:period]) / period
    for v in values[period:]:
        e = v * k + e * (1 - k)
    return e


def swings(candles: list, n: int = 2) -> tuple[list, list]:
    """(swing highs, swing lows) as [(index, price)], oldest first. A swing
    needs `n` bars either side, so the last `n` bars can never hold one.

    Ties go to the FIRST bar: strictly beyond everything on the left, at
    least level with everything on the right. Counting both bars of an equal
    pair made two swings at one price, and the second-to-last and last swing
    then compared equal, so a clean staircase read as "no trend"."""
    highs, lows = [], []
    for i in range(n, len(candles) - n):
        h = float(candles[i]["high"])
        lo = float(candles[i]["low"])
        left, right = candles[i - n:i], candles[i + 1:i + n + 1]
        if all(h > float(c["high"]) for c in left) and all(h >= float(c["high"]) for c in right):
            highs.append((i, h))
        if all(lo < float(c["low"]) for c in left) and all(lo <= float(c["low"]) for c in right):
            lows.append((i, lo))
    return highs, lows


def trend(h4: list, p: dict) -> tuple[str, dict]:
    """("up" | "down" | "none", facts). Structure first, EMA second."""
    highs, lows = swings(h4, p["pivot_n"])
    facts: dict = {}
    if len(highs) < 2 or len(lows) < 2 or not h4:
        return "none", facts
    (_, h1), (_, h2) = highs[-2], highs[-1]
    (_, l1), (_, l2) = lows[-2], lows[-1]
    close = float(h4[-1]["close"])
    a = atr(h4, p["atr_period"]) or 1e-9
    e = ema([float(c["close"]) for c in h4], p["ema_period"])
    facts = {"atr_h4": a, "ema": e, "close": close,
             "step_atr": ((h2 - h1) + (l2 - l1)) / 2.0 / a}
    if h2 > h1 and l2 > l1 and close > l2:
        direction = "up"
    elif h2 < h1 and l2 < l1 and close < h2:
        direction = "down"
    else:
        return "none", facts
    if p["require_ema"]:
        if e is None:
            return "none", facts
        if (direction == "up") != (close > e):
            return "none", facts
    return direction, facts


def session_of(now_utc: datetime, p: dict) -> Optional[str]:
    """The session the moment falls in, or None when trading is off."""
    h = now_utc.hour
    if now_utc.weekday() >= 5:
        return None
    if now_utc.weekday() == 4 and h >= p["friday_cutoff_utc"]:
        return None
    if not (p["session_start_utc"] <= h < p["session_end_utc"]):
        return None
    if 13 <= h < 16:
        return "overlap"
    return "london" if h < 13 else "new_york"


def levels(h1: list, direction: str, price: float, p: dict) -> list[tuple[float, str]]:
    """Support below price in an uptrend, resistance above it in a downtrend.

    Both kinds of swing count. A swing LOW below price is plain support; a
    swing HIGH below price is old resistance price has broken through, which
    the owner's step 3 names: "recent swing high turned floor".
    """
    window = h1[-p["level_lookback_h1"]:]
    highs, lows = swings(window, p["pivot_n"])
    out = []
    for _, lv in lows:
        if (direction == "BUY" and lv < price) or (direction == "SELL" and lv > price):
            out.append((lv, "swing_low"))
    for _, lv in highs:
        if (direction == "BUY" and lv < price) or (direction == "SELL" and lv > price):
            out.append((lv, "swing_high"))
    return out


def pattern(prev: dict, cur: dict, direction: str, p: dict) -> Optional[str]:
    """"engulfing", "pin" or None, for a candle pointing in `direction`."""
    o, h, l, c = (float(cur[k]) for k in ("open", "high", "low", "close"))
    po, pc = float(prev["open"]), float(prev["close"])
    rng = h - l
    if rng <= 0:
        return None
    body = abs(c - o)
    if direction == "BUY":
        if pc < po and c > o and c >= po and o <= pc and body > abs(pc - po):
            return "engulfing"
        wick = min(o, c) - l
    else:
        if pc > po and c < o and c <= po and o >= pc and body > abs(pc - po):
            return "engulfing"
        wick = h - max(o, c)
    if wick >= p["pin_wick_frac"] * rng and wick >= p["pin_wick_body"] * max(body, 1e-9):
        return "pin"
    return None


# ── the decision ─────────────────────────────────────────────────────────────

def evaluate(h4: list, h1: list, m15: list, now_utc: datetime,
             params: Optional[dict] = None) -> Union[Setup, str]:
    """A Setup, or the reason there is none. Closed candles only."""
    p = {**DEFAULTS, **(params or {})}
    session = session_of(now_utc, p)
    if session is None:
        return "outside London/New York"
    need = max(p["pullback_lookback"], p["sl_lookback"], p["atr_period"]) + 2
    if len(m15) < need or len(h1) < 2 * p["pivot_n"] + 3 or len(h4) < p["ema_period"]:
        return "not enough history"

    t, tf = trend(h4, p)
    if t == "none":
        return "no clear H4 trend"
    direction = "BUY" if t == "up" else "SELL"

    cur, prev = m15[-1], m15[-2]
    kind = pattern(prev, cur, direction, p)
    if kind is None:
        return f"{t}trend, no {direction.lower()} confirmation candle"

    a = atr(m15, p["atr_period"])
    if a <= 0:
        return "no volatility"
    o, h, l, c = (float(cur[k]) for k in ("open", "high", "low", "close"))
    if h - l > p["max_candle_atr"] * a:
        return f"confirmation candle {(h - l) / a:.1f}x ATR -- chasing"

    # Where did price come back FROM? Step 3: let price return to you.
    before = m15[-1 - p["pullback_lookback"]:-1]
    if direction == "BUY":
        came_from = max(float(x["high"]) for x in before)
        depth = came_from - l
    else:
        came_from = min(float(x["low"]) for x in before)
        depth = h - came_from
    if depth < p["min_pullback_atr"] * a:
        return f"no pullback ({depth / a:.1f}x ATR)"

    # `levels` only returns levels the close is already beyond, so a candle
    # that closed back through its level never gets here: the level held.
    best = None
    for lv, lk in levels(h1, direction, c, p):
        if direction == "BUY":
            touched = l <= lv + p["level_tol_atr"] * a and l >= lv - p["max_pierce_atr"] * a
            beyond = c - lv
        else:
            touched = h >= lv - p["level_tol_atr"] * a and h <= lv + p["max_pierce_atr"] * a
            beyond = lv - c
        if touched and (best is None or beyond < best[2]):
            best = (lv, lk, beyond)
    if best is None:
        return f"{kind} not at an H1 {'support' if direction == 'BUY' else 'resistance'}"
    level, level_kind, beyond = best
    if beyond > p["max_chase_atr"] * a:
        return f"close {beyond / a:.1f}x ATR past the level -- chasing"

    tail = m15[-p["sl_lookback"]:]
    if direction == "BUY":
        sl = min(float(x["low"]) for x in tail) - p["sl_buffer_atr"] * a
        risk = c - sl
    else:
        sl = max(float(x["high"]) for x in tail) + p["sl_buffer_atr"] * a
        risk = sl - c
    if risk < p["min_sl_atr"] * a:
        return f"stop {risk / a:.2f}x ATR -- too tight"
    if risk > p["max_sl_atr"] * a:
        return f"stop {risk / a:.2f}x ATR -- too wide"
    tp = c + p["rr"] * risk if direction == "BUY" else c - p["rr"] * risk

    sign = 1.0 if direction == "BUY" else -1.0
    rng = h - l
    wick = (min(o, c) - l) if direction == "BUY" else (h - max(o, c))
    hour = now_utc.hour + now_utc.minute / 60.0
    ema_v = tf.get("ema")
    features = {
        "trend_step_atr": sign * tf.get("step_atr", 0.0),
        "ema_dist_atr": sign * ((tf["close"] - ema_v) / tf["atr_h4"]) if ema_v else 0.0,
        "body_frac": abs(c - o) / rng,
        "wick_frac": wick / rng,
        "is_engulfing": 1.0 if kind == "engulfing" else 0.0,
        "level_dist_atr": beyond / a,
        "level_role_reversal": 1.0 if level_kind == ("swing_high" if direction == "BUY" else "swing_low") else 0.0,
        "pullback_atr": depth / a,
        "risk_atr": risk / a,
        "atr_ratio": a / (tf.get("atr_h4") or a),
        "hour_sin": math.sin(2 * math.pi * hour / 24),
        "hour_cos": math.cos(2 * math.pi * hour / 24),
        "overlap": 1.0 if session == "overlap" else 0.0,
        "is_buy": 1.0 if direction == "BUY" else 0.0,
    }
    return Setup(direction=direction, pattern=kind, entry=c, stop_loss=sl,
                 take_profit=tp, risk=risk, level=level, level_kind=level_kind,
                 session=session, atr_m15=a, atr_h4=tf.get("atr_h4", 0.0),
                 features=features)


def broker_ts_to_utc(ts: float, offset_s: int) -> datetime:
    return datetime.fromtimestamp(float(ts) - offset_s, tz=timezone.utc)
