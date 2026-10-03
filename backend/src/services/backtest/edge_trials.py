"""The 2025 edge trials and the bar they are judged by.

docs/todo/reversal-engine/270, pre-registered before any of this ran on 2025.
Pure: bars in, trades and verdicts out. No broker, no database, no clock.

Where a trial is an existing strategy (the New York ORB, Trend PA), the
PRODUCTION code decides; this module only steps time forward and settles the
trade. T7-T9 are new rules written from the literature named in the spec, and
are the only trading logic here.

A trade is a dict with at least `ts` (decision time, UTC seconds), `r` (its
result after costs: R where the rule has a stop, points where it has none)
and `side` (+1 long, -1 short).
"""
from __future__ import annotations

import math
from collections import defaultdict
from datetime import datetime, timezone
from statistics import NormalDist
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

from backend.src.services.analytics import orb_ny
from backend.src.services.reversal_engine.entry_study import Bar
from backend.src.services.trend_pa import outcome as oc

NY = ZoneInfo("America/New_York")
N_TRIALS = 9
# One-sided Bonferroni at 5% for the nine pre-registered trials.
T_BAR = NormalDist().inv_cdf(1 - 0.05 / N_TRIALS)
MIN_N = 50
HALF_SPLIT_2025 = datetime(2025, 7, 1, tzinfo=timezone.utc).timestamp()


def as_dicts(bars: Sequence[Bar]) -> list[dict]:
    """Bars in the candle-dict shape the production strategies read."""
    return [{"ts": b.ts, "time": b.ts, "open": b.open, "high": b.high,
             "low": b.low, "close": b.close, "volume": b.volume} for b in bars]


# ── The bar ─────────────────────────────────────────────────────────────────

def _day(ts: float) -> int:
    return int(ts // 86400)


def clustered_t(trades: Sequence[dict]) -> Optional[float]:
    """Mean over its standard error, with trades on the same UTC day allowed
    to move together. Ten trades on one day are one day's evidence."""
    n = len(trades)
    if n < 2:
        return None
    mean = sum(t["r"] for t in trades) / n
    resid: dict = defaultdict(float)
    for t in trades:
        resid[_day(t["ts"])] += t["r"] - mean
    var = sum(v * v for v in resid.values())
    if var <= 0:
        return None
    return mean / (math.sqrt(var) / n)


def judge(trades: Sequence[dict], split: float = HALF_SPLIT_2025) -> dict:
    """The spec's candidate bar: n >= 50, positive mean after costs in both
    halves, clustered one-sided t >= T_BAR."""
    n = len(trades)
    mean = sum(t["r"] for t in trades) / n if n else float("nan")
    h1 = [t["r"] for t in trades if t["ts"] < split]
    h2 = [t["r"] for t in trades if t["ts"] >= split]
    m1 = sum(h1) / len(h1) if h1 else float("nan")
    m2 = sum(h2) / len(h2) if h2 else float("nan")
    t = clustered_t(trades)
    sd = (math.sqrt(sum((x["r"] - mean) ** 2 for x in trades) / (n - 1))
          if n > 1 else float("nan"))
    why = []
    if n < MIN_N:
        why.append(f"n {n} < {MIN_N}")
    if not (m1 > 0 and m2 > 0):
        why.append(f"half: {m1:+.3f} / {m2:+.3f} not positive in both")
    if t is None or t < T_BAR:
        why.append(f"t {t if t is not None else float('nan'):+.2f} < {T_BAR:.2f}")
    return {"n": n, "mean": mean, "sd": sd, "h1": m1, "h2": m2, "t": t,
            "days": len({_day(x["ts"]) for x in trades}),
            "candidate": not why, "why": why}


# ── T3 / T4: the production New York ORB ───────────────────────────────────

_UP = [1.0] * 50 + [2.0]      # closes whose last bar is above its EMA50
_DOWN = [2.0] * 50 + [1.0]


def _settle(side: int, entry: float, stop: float, target: float, after: list,
            opened: float, cost: float, max_hold_s: Optional[float] = None) -> Optional[dict]:
    res = oc.resolve_bars("BUY" if side > 0 else "SELL", entry, stop, target, after,
                          opened_ts=opened, max_hold_s=max_hold_s)
    if res is None:
        return None
    risk = abs(entry - stop)
    r = (side * (float(res["exit_price"]) - entry) - cost) / risk
    return {"ts": opened, "side": side, "entry": entry, "stop": stop,
            "target": target, "exit": float(res["exit_price"]),
            "outcome": res["outcome"], "r": r}


def orb_ny_trades(m5: Sequence[Bar], h4: Sequence[Bar], trend_filter: bool = True,
                  cost: float = 0.30) -> list[dict]:
    """`orb_ny.evaluate` asked at every M5 close of every New York morning,
    as the live report would be; the first `signal` is filled at that close
    and held to its stop or 2R target. With `trend_filter` off, the H4 trend
    it is shown is whichever one agrees with the breakout."""
    m5 = sorted(m5, key=lambda b: b.ts)
    h4_ends = [b.ts + 14400 for b in h4]
    by_day: dict = defaultdict(list)
    for b in m5:
        by_day[datetime.fromtimestamp(b.ts, NY).date()].append(b)
    all_dicts = as_dicts(m5)
    idx = {b.ts: i for i, b in enumerate(m5)}
    trades = []
    for day in sorted(by_day):
        bars = by_day[day]
        noon = datetime(day.year, day.month, day.day, 12, tzinfo=NY).astimezone(timezone.utc)
        or_end = orb_ny.open_utc(noon) + orb_ny.OR_MINUTES * 60
        day_dicts = as_dicts(bars)
        for b in bars:
            now = b.ts + orb_ny.BAR_S
            if now < or_end:
                continue
            k = 0
            while k < len(h4_ends) and h4_ends[k] <= now:
                k += 1
            real = [x.close for x in h4[max(0, k - orb_ny.H4_BARS):k]]
            rep = orb_ny.evaluate(day_dicts, real if trend_filter else _UP, now, b.close, b.close)
            if not trend_filter and rep["phase"] == "done" and "against the H4" in rep["position_note"]:
                rep = orb_ny.evaluate(day_dicts, _DOWN, now, b.close, b.close)
            if rep["phase"] == "signal":
                side = 1 if rep["direction"] == "bullish" else -1
                t = _settle(side, float(rep["current_price"]), float(rep["stop"]),
                            float(rep["target"]), all_dicts[idx[b.ts] + 1:], now, cost)
                if t:
                    trades.append(t)
                break
            if rep["phase"] == "done":
                break
    return trades


# ── T7: intraday momentum into the COMEX settlement ────────────────────────

def _ny_ts(d, h: int, m: int) -> float:
    return datetime(d.year, d.month, d.day, h, m, tzinfo=NY).timestamp()


def intraday_momentum(m1: Sequence[Bar], cost: float) -> list[dict]:
    """Sign of 08:20-08:50 ET, held 13:00-13:30 ET. Points after `cost`."""
    at = {b.ts: b for b in m1}
    days = sorted({datetime.fromtimestamp(b.ts, NY).date() for b in m1})
    out = []
    for d in days:
        if d.weekday() >= 5:
            continue
        o, c = at.get(_ny_ts(d, 8, 20)), at.get(_ny_ts(d, 8, 49))
        e, x = at.get(_ny_ts(d, 13, 0)), at.get(_ny_ts(d, 13, 29))
        if not (o and c and e and x):
            continue
        first = c.close - o.open
        if first == 0:
            continue
        side = 1 if first > 0 else -1
        out.append({"ts": e.ts, "side": side, "first": first,
                    "r": side * (x.close - e.open) - cost})
    return out


# ── T8: London sweeps the Asian range, fade it ─────────────────────────────

def asian_sweep_fade(m5: Sequence[Bar], cost: float) -> list[dict]:
    """The first M5 bar between 07:00 and 10:00 UTC that trades beyond the
    00:00-07:00 range decides the day: closed back inside, fade it (stop at
    its extreme, target 1R, out by 21:00 UTC); closed beyond, no trade."""
    m5 = sorted(m5, key=lambda b: b.ts)
    by_day: dict = defaultdict(list)
    for b in m5:
        by_day[_day(b.ts)].append(b)
    out = []
    for day, bars in sorted(by_day.items()):
        d0 = day * 86400.0
        asia = [b for b in bars if d0 <= b.ts < d0 + 7 * 3600]
        if len(asia) < 42:
            continue
        hi, lo = max(b.high for b in asia), min(b.low for b in asia)
        later = [b for b in bars if d0 + 7 * 3600 <= b.ts < d0 + 21 * 3600]
        for i, b in enumerate(later):
            if b.ts >= d0 + 10 * 3600:
                break
            up, down = b.high > hi, b.low < lo
            if not (up or down):
                continue
            if up and down:
                break
            inside = lo < b.close < hi
            if not inside:
                break
            side = -1 if up else 1
            entry, stop = b.close, (b.high if up else b.low)
            risk = abs(entry - stop)
            if risk <= 0:
                break
            target = entry + side * risk
            rest = as_dicts(later[i + 1:])
            t = _settle(side, entry, stop, target, rest, b.ts + 300, cost)
            if t is None and rest:
                last = rest[-1]["close"]
                t = {"ts": b.ts + 300, "side": side, "entry": entry, "stop": stop,
                     "target": target, "exit": last, "outcome": "timeout",
                     "r": (side * (last - entry) - cost) / risk}
            if t:
                out.append(t)
            break
    return out


# ── T9: daily time-series momentum ─────────────────────────────────────────

def daily_bars(m1: Sequence[Bar]) -> list[Bar]:
    """Gold days, rolling at 22:00 UTC (the futures day), stamped by the day
    they end on. Built here so a Sunday-evening open is part of Monday."""
    from backend.src.services.reversal_engine.historical_bars import aggregate
    shifted = [Bar(b.ts + 7200, b.open, b.high, b.low, b.close, b.volume) for b in m1]
    return aggregate(shifted, 86400)


def daily_momentum(days: Sequence[Bar], lookback: int, cost: float) -> list[dict]:
    """Long when the last `lookback` days closed up, short when down, held
    one day. Points after `cost`. Today's close is never an input."""
    out = []
    for i in range(lookback + 1, len(days)):
        past = days[i - 1].close - days[i - 1 - lookback].close
        if past == 0:
            continue
        side = 1 if past > 0 else -1
        out.append({"ts": days[i].ts, "side": side,
                    "r": side * (days[i].close - days[i - 1].close) - cost})
    return out


# ── Amendment A: the holdout bar (registered before 2019-2024 were read) ────

HOLDOUT_TRIALS = 3
HOLDOUT_T_BAR = NormalDist().inv_cdf(1 - 0.05 / HOLDOUT_TRIALS)
HOLDOUT_MIN_YEAR_N = 30


def holdout_judge(trades: Sequence[dict]) -> dict:
    """Pooled clustered t >= HOLDOUT_T_BAR, and a positive mean after costs
    in MORE than two thirds of the years that have >= 30 trades."""
    by_year: dict = defaultdict(list)
    for t in trades:
        by_year[datetime.fromtimestamp(t["ts"], timezone.utc).year].append(t["r"])
    years = {y: {"n": len(v), "mean": sum(v) / len(v)} for y, v in sorted(by_year.items())}
    counted = [y for y, v in years.items() if v["n"] >= HOLDOUT_MIN_YEAR_N]
    pos = [y for y in counted if years[y]["mean"] > 0]
    n = len(trades)
    mean = sum(t["r"] for t in trades) / n if n else float("nan")
    t = clustered_t(trades)
    why = []
    if t is None or t < HOLDOUT_T_BAR:
        why.append(f"t {t if t is not None else float('nan'):+.2f} < {HOLDOUT_T_BAR:.2f}")
    if not counted or len(pos) / len(counted) <= 2.0 / 3.0:
        why.append(f"year: positive in {len(pos)} of {len(counted)} years with "
                   f">= {HOLDOUT_MIN_YEAR_N} trades, needs more than two thirds")
    return {"n": n, "mean": mean, "t": t, "years": years, "counted_years": counted,
            "passes": not why, "why": why}


def reverse_points(trades: Sequence[dict], cost: float) -> list[dict]:
    """The same decisions taken the other way. `r` was side * move - cost, so
    the reverse is -side * move - cost: the cost is paid either way."""
    return [{**t, "side": -t["side"], "r": -(t["r"] + cost) - cost} for t in trades]
