"""Dukascopy tick files to M1 bars, and the resumable download that fetches them.

docs/todo/reversal-engine/260. Research only: nothing here is on the trading
path, and the one network call is a function the caller injects, so this
module (and its tests) never opens a socket itself.

THE FILE FORMAT (checked against a real hour of XAUUSD ticks, 2026-10-01)
-------------------------------------------------------------------------
`https://datafeed.dukascopy.com/datafeed/<SYMBOL>/<year>/<month>/<day>/<hour>h_ticks.bi5`
with the MONTH ZERO-BASED (January is 00). The body is LZMA; inside it each
tick is 20 big-endian bytes: uint32 milliseconds since the hour began, uint32
ask, uint32 bid, float32 ask volume, float32 bid volume. Gold's prices are
integers over 1000 (2077255 is 2077.255). Ask before bid is the trap: read
them the other way round and the spread is negative.

BARS ARE BID, TIMES ARE UTC
---------------------------
Same as MT5's own bars, so a replay on them is comparable. Volume is the tick
count, which is what MT5 calls tick volume; the study's volume features are
ratios, so the scale does not matter. Dukascopy stamps true UTC where the
bridge stamps broker time (the engines README, "The bridge stamps bars in
broker time"), which is the cleaner of the two for a session-aware engine.
"""
from __future__ import annotations

import lzma
import struct
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, NamedTuple, Optional, Sequence

from backend.src.services.reversal_engine.entry_study import Bar

_TICK = struct.Struct(">IIIff")
_HOUR_S = 3600
_BASE_URL = "https://datafeed.dukascopy.com/datafeed"


class Tick(NamedTuple):
    ts: float
    bid: float
    ask: float


def decode_bi5(blob: bytes, hour_start: float, divisor: float = 1000.0) -> list[Tick]:
    """Ticks in one hour file. An empty body is an hour with no trading."""
    if not blob:
        return []
    raw = lzma.decompress(blob)
    out = []
    for ms, ask, bid, _av, _bv in _TICK.iter_unpack(raw[: len(raw) - len(raw) % _TICK.size]):
        out.append(Tick(hour_start + ms / 1000.0, bid / divisor, ask / divisor))
    return out


def ticks_to_m1(ticks: Sequence[Tick]) -> list[Bar]:
    """Bid OHLC per minute, volume = ticks in the minute. A minute with no
    tick has no bar; nothing is interpolated."""
    out: list[Bar] = []
    cur_ts: Optional[float] = None
    o = h = l = c = 0.0
    n = 0
    for t in sorted(ticks, key=lambda t: t.ts):
        m = (t.ts // 60) * 60.0
        if m != cur_ts:
            if cur_ts is not None:
                out.append(Bar(cur_ts, o, h, l, c, float(n)))
            cur_ts, o, h, l, c, n = m, t.bid, t.bid, t.bid, t.bid, 0
        h = max(h, t.bid)
        l = min(l, t.bid)
        c = t.bid
        n += 1
    if cur_ts is not None:
        out.append(Bar(cur_ts, o, h, l, c, float(n)))
    return out


def aggregate(bars: Sequence[Bar], seconds: int) -> list[Bar]:
    """Coarser bars on epoch-aligned buckets. Input must be oldest first.

    UTC-aligned: an H4 bucket starts at 00, 04, 08 UTC, where a broker's H4
    starts on its own server clock. The engine reads only the last six H4
    bars for a bias, so the offset moves where the bucket edges fall, not
    what they say.
    """
    out: list[Bar] = []
    cur: Optional[float] = None
    o = h = l = c = v = 0.0
    for b in bars:
        k = (b.ts // seconds) * float(seconds)
        if k != cur:
            if cur is not None:
                out.append(Bar(cur, o, h, l, c, v))
            cur, o, h, l, c, v = k, b.open, b.high, b.low, b.close, 0.0
        h = max(h, b.high)
        l = min(l, b.low)
        c = b.close
        v += b.volume
    if cur is not None:
        out.append(Bar(cur, o, h, l, c, v))
    return out


# ── The download ──────────────────────────────────────────────────────────

def hour_url(symbol: str, hour_start: float) -> str:
    d = datetime.fromtimestamp(hour_start, timezone.utc)
    return f"{_BASE_URL}/{symbol}/{d.year}/{d.month - 1:02d}/{d.day:02d}/{d.hour:02d}h_ticks.bi5"


def hour_path(root: Path, symbol: str, hour_start: float) -> Path:
    d = datetime.fromtimestamp(hour_start, timezone.utc)
    return Path(root) / symbol / f"{d.year}" / f"{d.month:02d}" / f"{d.day:02d}" / f"{d.hour:02d}h.bi5"


def _market_closed(hour_start: float) -> bool:
    """Hours the gold market is certainly shut, so asking for them is wasted
    requests: all of Saturday, Sunday before 21:00 UTC, Friday from 22:00.
    Deliberately wider than the real session so a DST shift loses nothing."""
    d = datetime.fromtimestamp(hour_start, timezone.utc)
    wd = d.weekday()
    return wd == 5 or (wd == 6 and d.hour < 21) or (wd == 4 and d.hour >= 22)


class RateLimited(Exception):
    """The server said slow down (HTTP 429). Raised by a `fetch`, handled by
    `download_range`: wait, retry the same hour, and stop the run if it keeps
    happening. Not a failure of the hour and never recorded as "no data"."""

    def __init__(self, retry_after_s: float = 60.0):
        super().__init__(f"rate limited, retry after {retry_after_s:g}s")
        self.retry_after_s = float(retry_after_s)


@dataclass
class DownloadReport:
    fetched: int = 0
    cached: int = 0
    empty: int = 0
    skipped_closed: int = 0
    failed: list = field(default_factory=list)
    rate_limited: bool = False
    deferred: int = 0


def download_range(fetch: Callable[[str], Optional[bytes]], root: Path, symbol: str,
                   start_ts: float, end_ts: float, workers: int = 1,
                   retries: int = 2, pause_s: float = 0.0,
                   max_rate_limited: int = 5,
                   sleep: Callable[[float], None] = time.sleep) -> DownloadReport:
    """Fetch every missing hour in [start_ts, end_ts) into `root`.

    `fetch(url)` returns the body, None for "no such file", raises
    `RateLimited` for HTTP 429, and raises anything else for a failure.

    - A failure is retried, then reported, and nothing is written, so the next
      run asks again. Only a definite "no data" leaves a zero-byte marker,
      which is what stops a weekend or a holiday being asked for on every run.
    - A rate limit is waited out (the server's own `Retry-After`) and the same
      hour retried. `max_rate_limited` of them IN A ROW stops the whole run
      and leaves the rest for next time: a server that keeps saying no is
      not helped by being asked again. One success resets the count.
    - Resumable by construction: an hour already on disk is never fetched.
    """
    import threading
    report = DownloadReport()
    first = (start_ts // _HOUR_S) * _HOUR_S
    todo = []
    t = first
    while t < end_ts:
        if _market_closed(t):
            report.skipped_closed += 1
        elif hour_path(root, symbol, t).exists():
            report.cached += 1
        else:
            todo.append(t)
        t += _HOUR_S

    lock = threading.Lock()
    state = {"in_a_row": 0, "stop": False}

    def one(hour: float):
        url = hour_url(symbol, hour)
        err: Optional[Exception] = None
        attempt = 0
        while True:
            if state["stop"]:
                return hour, "deferred", ""
            try:
                body = fetch(url)
                with lock:
                    state["in_a_row"] = 0
                break
            except RateLimited as rl:
                with lock:
                    state["in_a_row"] += 1
                    if state["in_a_row"] > max_rate_limited:
                        state["stop"] = True
                        return hour, "deferred", ""
                sleep(rl.retry_after_s)
            except Exception as exc:  # noqa: BLE001 - every failure is a retry
                err = exc
                if attempt >= retries:
                    return hour, "failed", str(err)
                sleep(0.5 * (2 ** attempt))
                attempt += 1
        p = hour_path(root, symbol, hour)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(body or b"")
        tmp.replace(p)
        if pause_s:
            sleep(pause_s)
        return hour, ("fetched" if body else "empty"), ""

    with ThreadPoolExecutor(max_workers=max(1, workers)) as ex:
        for hour, status, msg in ex.map(one, todo):
            if status == "failed":
                report.failed.append((hour, msg))
            elif status == "fetched":
                report.fetched += 1
            elif status == "deferred":
                report.deferred += 1
            else:
                report.empty += 1
    report.rate_limited = state["stop"]
    return report


def load_m1(root: Path, symbol: str, start_ts: float, end_ts: float) -> list[Bar]:
    """M1 bars for every downloaded hour in [start_ts, end_ts), oldest first."""
    out: list[Bar] = []
    t = (start_ts // _HOUR_S) * _HOUR_S
    while t < end_ts:
        p = hour_path(root, symbol, t)
        if p.exists() and p.stat().st_size:
            out.extend(ticks_to_m1(decode_bi5(p.read_bytes(), t)))
        t += _HOUR_S
    return out
