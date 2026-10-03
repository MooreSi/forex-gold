"""Bucket signal outcomes by signed distance to a scheduled macro event.

RESEARCH ONLY. Nothing here is read by a live path. It answers one question
before anyone builds an "event radar" feature: are signals taken just AFTER a
release (spread wide, book thin, stops slipping) measurably worse than signals
taken far from any event? `news_proximity_norm` only looks forward, so the
post-release side is the part the models cannot see today.

Event times come from two places, both explicit:
  * `scheduled_events` -- rules that need no feed: NFP (first Friday, 08:30
    New York) and the FOMC statement (14:00 New York on the days in
    `news_calendar._FOMC_DATES`). The BLS occasionally moves NFP off the first
    Friday; those weeks are wrong here and `--events` is how to correct them.
  * `load_events_csv` -- anything else (CPI, PPI, retail sales, ...). The live
    feed only publishes the current week, so there is no history to read, and
    this module does not guess CPI dates.

A bucket reports how many DISTINCT EVENTS fed it. Hundreds of signals around
one FOMC statement are a single observation about FOMC.
"""
from __future__ import annotations

import csv
import math
import statistics
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from pathlib import Path
from typing import Optional, Sequence
from zoneinfo import ZoneInfo

_NY = ZoneInfo("America/New_York")
_UTC = timezone.utc

EDGES = (-120, -60, -30, -15, 0, 15, 30, 60, 120)
NONE = "none"
LABELS = tuple(f"{lo}..{hi}" for lo, hi in zip(EDGES, EDGES[1:])) + (NONE,)

# Below either of these a bucket is reported but flagged, not trusted.
MIN_SIGNALS = 30
MIN_EVENTS = 3


@dataclass(frozen=True)
class Event:
    when: datetime      # tz-aware UTC
    tier: int
    title: str


def _ny_to_utc(d: date, hh: int, mm: int) -> datetime:
    return datetime.combine(d, dtime(hh, mm), tzinfo=_NY).astimezone(_UTC)


def scheduled_events(start: datetime, end: datetime) -> list[Event]:
    """NFP and FOMC statements with `start <= when <= end`. Tier 1 both."""
    from backend.src.utils.news_calendar import _FOMC_DATES
    out: list[Event] = []
    d = start.date() - timedelta(days=1)
    last = end.date() + timedelta(days=1)
    while d <= last:
        if d.weekday() == 4 and d.day <= 7:
            out.append(Event(_ny_to_utc(d, 8, 30), 1, "NFP"))
        if (d.month, d.day) in _FOMC_DATES.get(d.year, set()):
            out.append(Event(_ny_to_utc(d, 14, 0), 1, "FOMC"))
        d += timedelta(days=1)
    return sorted((e for e in out if start <= e.when <= end), key=lambda e: e.when)


def load_events_csv(path) -> list[Event]:
    """`utc,tier,title` rows. A timestamp without an offset is refused: a
    silent guess at the zone moves every event by hours."""
    out = []
    with open(Path(path), newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            when = datetime.fromisoformat(row["utc"].strip())
            if when.tzinfo is None:
                raise ValueError(f"timestamp has no UTC offset: {row['utc']!r}")
            out.append(Event(when.astimezone(_UTC), int(row["tier"]), row["title"].strip()))
    return sorted(out, key=lambda e: e.when)


def nearest(ts: float, events: Sequence[Event]) -> tuple[Optional[float], Optional[Event]]:
    """Signed minutes from the NEAREST event to `ts`: positive means the signal
    is after the release, negative before it."""
    best, best_e = None, None
    for e in events:
        d = (ts - e.when.timestamp()) / 60.0
        if best is None or abs(d) < abs(best):
            best, best_e = d, e
    return best, best_e


def bucket_of(minutes: Optional[float]) -> str:
    if minutes is None:
        return NONE
    for lo, hi in zip(EDGES, EDGES[1:]):
        if lo <= minutes < hi:
            return f"{lo}..{hi}"
    return NONE


@dataclass(frozen=True)
class Bucket:
    label: str
    n: int
    events: int
    mean_r: float
    median_r: float
    win_rate: float
    se: Optional[float]
    diff_vs_none: Optional[float]
    t_vs_none: Optional[float]
    thin: bool


def _se(xs: list) -> Optional[float]:
    return statistics.stdev(xs) / math.sqrt(len(xs)) if len(xs) > 1 else None


def summarise(rows: Sequence[dict], events: Sequence[Event]) -> list[Bucket]:
    """`rows` are `{"ts": unix seconds, "r": realised R or None}`."""
    groups: dict = {label: ([], set()) for label in LABELS}
    for row in rows:
        if row.get("r") is None:
            continue
        d, e = nearest(float(row["ts"]), events)
        label = bucket_of(d)
        groups[label][0].append(float(row["r"]))
        if label != NONE and e is not None:
            groups[label][1].add(e.when)
    base = groups[NONE][0]
    base_mean, base_se = (statistics.fmean(base) if base else None), _se(base)
    out = []
    for label in LABELS:
        rs, evs = groups[label]
        if not rs:
            continue
        mean = statistics.fmean(rs)
        se = _se(rs)
        diff = (mean - base_mean) if (base_mean is not None and label != NONE) else None
        t = None
        if diff is not None and se is not None and base_se is not None:
            denom = math.sqrt(se ** 2 + base_se ** 2)
            t = diff / denom if denom > 0 else None
        out.append(Bucket(
            label=label, n=len(rs), events=len(evs), mean_r=mean,
            median_r=statistics.median(rs),
            win_rate=sum(1 for r in rs if r > 0) / len(rs),
            se=se, diff_vs_none=diff, t_vs_none=t,
            thin=(len(rs) < MIN_SIGNALS) or (label != NONE and len(evs) < MIN_EVENTS),
        ))
    return out
