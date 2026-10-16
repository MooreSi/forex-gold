"""The session-drift trials and the bar they are judged by.

docs/todo/018, pre-registered before any of this ran. Pure: daily M1 bars in,
trades and verdicts out. No broker, no database, no clock, no socket.

    D1  Asian long: every weekday, 00:00 to 06:59 UTC
    D2  Friday long: 00:00 to 19:59 UTC
    D3  200-day direction: each weekday, the side yesterday's close takes
        against the SMA200 of the 200 trading days up to yesterday
    D4  D1, only on the days D3 is long

A trade is a dict with `ts` (the UTC day it was taken), `r` (points after
cost) and `side`.

THE CANDLE FILE (checked 2026-10-14 against the tick-built bars, spec 018
Amendment 1): `<SYMBOL>/<year>/<month0>/<day>/BID_candles_min_1.bi5`, LZMA,
24-byte big-endian records: uint32 seconds into the UTC day, then OPEN,
CLOSE, LOW, HIGH as uint32 over 1000, then float32 volume. Read as O,H,L,C
and most minutes still look plausible; they are wrong.
"""
from __future__ import annotations

import lzma
import struct
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist
from typing import Mapping, Optional, Sequence

from backend.src.services.backtest.edge_trials import clustered_t
from backend.src.services.reversal_engine.entry_study import Bar

N_TRIALS = 4
# One-sided Bonferroni at 5% for the four pre-registered trials.
T_BAR = NormalDist().inv_cdf(1 - 0.05 / N_TRIALS)
MIN_YEAR_N = 30
SPLIT_YEAR = 2019
BEAR_YEARS = (2013, 2014, 2015)
MAX_MISSING = 0.10          # D1, D2: share of the window's HOURS with no tick
MIN_TRADED_HOURS = 12       # D3: of a weekday's 24 (spec 018 Amendment 2)
SMA_DAYS = 200

_REC = struct.Struct(">IIIIIf")
_BASE_URL = "https://datafeed.dukascopy.com/datafeed"
_DAY_S = 86400


def _utc(ts: float) -> datetime:
    return datetime.fromtimestamp(ts, timezone.utc)


def candle_url(symbol: str, day_ts: float) -> str:
    d = _utc(day_ts)
    return f"{_BASE_URL}/{symbol}/{d.year}/{d.month - 1:02d}/{d.day:02d}/BID_candles_min_1.bi5"


def candle_path(root: Path, symbol: str, day_ts: float) -> Path:
    d = _utc(day_ts)
    return root / symbol / f"{d.year}" / f"{d.month:02d}" / f"{d.day:02d}_BID_m1.bi5"


def decode_candles(blob: bytes, day_ts: float, divisor: float = 1000.0) -> list[Bar]:
    """Every minute record in a day's candle file, untraded minutes included."""
    if not blob:
        return []
    raw = lzma.decompress(blob)
    out = []
    for i in range(0, len(raw) - len(raw) % _REC.size, _REC.size):
        sec, o, c, lo, hi, vol = _REC.unpack_from(raw, i)
        out.append(Bar(day_ts + sec, o / divisor, hi / divisor, lo / divisor,
                       c / divisor, float(vol)))
    return out


# ── D1, D2: a fixed window each day ─────────────────────────────────────────

def _traded_hours(bars) -> set:
    return {int(b.ts // 3600) for b in bars if b.volume > 0}


def _window_trade(day_ts: float, bars: Sequence[Bar], start_min: int, end_min: int,
                  cost: float) -> Optional[dict]:
    """Long from the open of minute `start_min` to the close of minute
    `end_min - 1`. None when more than MAX_MISSING of the window's hours have
    no traded minute, or its first or last minute record is absent. Counted
    in hours, not minutes: 2003's Asian hours trade 7-13 minutes in 60, which
    is a quiet market, not missing data (spec 018 Amendment 2)."""
    lo, hi = day_ts + start_min * 60, day_ts + end_min * 60
    inside = {b.ts: b for b in bars if lo <= b.ts < hi}
    hours = (end_min - start_min) // 60
    missing = hours - len(_traded_hours(inside.values()))
    first, last = inside.get(lo), inside.get(hi - 60)
    if first is None or last is None or missing > MAX_MISSING * hours:
        return None
    return {"ts": day_ts, "r": last.close - first.open - cost, "side": 1}


def _each_day(days: Mapping[float, Sequence[Bar]], weekdays: set[int], start_min: int,
              end_min: int, cost: float) -> tuple[list[dict], int]:
    trades, dropped = [], 0
    for day_ts in sorted(days):
        if _utc(day_ts).weekday() not in weekdays:
            continue
        t = _window_trade(day_ts, days[day_ts], start_min, end_min, cost)
        if t is None:
            dropped += 1
        else:
            trades.append(t)
    return trades, dropped


def asian_long(days: Mapping[float, Sequence[Bar]], cost: float) -> tuple[list[dict], int]:
    """D1. Returns (trades, days dropped for missing minutes)."""
    return _each_day(days, {0, 1, 2, 3, 4}, 0, 7 * 60, cost)


def friday_long(days: Mapping[float, Sequence[Bar]], cost: float) -> tuple[list[dict], int]:
    """D2. Returns (trades, days dropped for missing minutes)."""
    return _each_day(days, {4}, 0, 20 * 60, cost)


# ── D3, D4: the 200-day direction ──────────────────────────────────────────

def daily_bars(days: Mapping[float, Sequence[Bar]]) -> tuple[list[tuple], int]:
    """(day_ts, open, close) for every weekday with at least MIN_TRADED_HOURS
    traded hours, oldest first, and the count of weekdays dropped. Open is the 00:00 record,
    close the last traded minute's close."""
    rows, dropped = [], 0
    for day_ts in sorted(days):
        if _utc(day_ts).weekday() >= 5:
            continue
        bars = sorted(days[day_ts], key=lambda b: b.ts)
        traded = [b for b in bars if b.volume > 0]
        first = bars[0] if bars and bars[0].ts == day_ts else None
        if first is None or len(_traded_hours(traded)) < MIN_TRADED_HOURS:
            dropped += 1
            continue
        rows.append((day_ts, first.open, traded[-1].close))
    return rows, dropped


def sma200_trades(rows: Sequence[tuple], cost: float) -> list[dict]:
    """D3. Day i takes the side of close[i-1] against mean(close[i-200..i-1])."""
    out = []
    closes = [r[2] for r in rows]
    for i in range(SMA_DAYS, len(rows)):
        sma = sum(closes[i - SMA_DAYS:i]) / SMA_DAYS
        side = 1 if closes[i - 1] > sma else -1
        day_ts, o, c = rows[i]
        out.append({"ts": day_ts, "r": side * (c - o) - cost, "side": side})
    return out


def asian_above_sma(asian: Sequence[dict], d3: Sequence[dict]) -> list[dict]:
    """D4: D1's trades on the days D3 is long."""
    long_days = {t["ts"] for t in d3 if t["side"] == 1}
    return [t for t in asian if t["ts"] in long_days]


def bear_window_ok(rows: Sequence[tuple]) -> tuple[bool, dict]:
    """Each BEAR_YEARS year closes below the year before. Checked before any
    trial result is read."""
    closes: dict = {}
    for day_ts, _o, c in rows:
        closes[_utc(day_ts).year] = c
    ok = all(y in closes and (y - 1) in closes and closes[y] < closes[y - 1]
             for y in BEAR_YEARS)
    return ok, closes


# ── The bar ─────────────────────────────────────────────────────────────────

def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def verdict(trades: Sequence[dict]) -> dict:
    """Confirmed only if every condition in spec 018 section 2 holds."""
    by_year: dict = defaultdict(list)
    for t in trades:
        by_year[_utc(t["ts"]).year].append(t["r"])
    year_mean = {y: _mean(rs) for y, rs in sorted(by_year.items())}
    voting = [y for y, rs in by_year.items() if len(rs) >= MIN_YEAR_N]
    positive = [y for y in voting if year_mean[y] > 0]
    bear = _mean([r for y in BEAR_YEARS for r in by_year.get(y, [])])
    pre = _mean([t["r"] for t in trades if _utc(t["ts"]).year < SPLIT_YEAR])
    post = _mean([t["r"] for t in trades if _utc(t["ts"]).year >= SPLIT_YEAR])
    t = clustered_t(trades)
    why = []
    if t is None or t < T_BAR:
        why.append(f"t {t if t is not None else float('nan'):+.2f} < {T_BAR:.2f}")
    if not (voting and len(positive) * 3 > len(voting) * 2):
        why.append(f"positive in {len(positive)} of {len(voting)} years, not > 2/3")
    if not bear > 0:
        why.append(f"bear window mean {bear:+.3f} not > 0")
    if not pre > 0:
        why.append(f"pre-{SPLIT_YEAR} mean {pre:+.3f} not > 0")
    if not post > 0:
        why.append(f"{SPLIT_YEAR}+ mean {post:+.3f} not > 0")
    return {"n": len(trades), "mean": _mean([x["r"] for x in trades]), "t": t,
            "year_mean": year_mean, "year_n": {y: len(rs) for y, rs in by_year.items()},
            "voting_years": sorted(voting), "positive_years": sorted(positive),
            "bear": bear, "pre": pre, "post": post,
            "confirmed": not why, "why": why}
