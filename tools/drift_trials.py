#!/usr/bin/env python3
"""docs/todo/018: the session-drift trials on Dukascopy daily M1 candles.

    D1  Asian long 00:00-06:59 UTC     D2  Friday long 00:00-19:59 UTC
    D3  200-day direction, daily       D4  D1 on the days D3 is long

Research only: downloads public candle files into its own cache and reads
them. No broker, no database, no orders.

Usage:
    .venv/bin/python -m tools.drift_trials download --start 2002-01-01 --end 2026-01-01
    .venv/bin/python -m tools.drift_trials run [--out results.json]

`run` checks the bear window BEFORE computing any trial and stops if it does
not hold, so the spec can be corrected without anyone having seen a result.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Optional

from backend.src.config import DATA_DIR
from backend.src.services.backtest import drift_trials as dt
from backend.src.services.market.validation import deflated_sharpe
from backend.src.services.reversal_engine import historical_bars as hb
from tools import re_dukascopy_replay as duka
from tools.edge_trials_2025 import COST

DEFAULT_CACHE = DATA_DIR / "research_dukascopy_candles"
SYMBOL = "XAUUSD"
END = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
NAMES = {"D1": "Asian long 00-07 UTC", "D2": "Friday long 00-20 UTC",
         "D3": "200-day direction", "D4": "Asian long, 200-day long days only"}


def _weekdays(start: float, end: float) -> Iterable[float]:
    t = start
    while t < end:
        if datetime.fromtimestamp(t, timezone.utc).weekday() < 5:
            yield t
        t += 86400


def cmd_download(a) -> int:
    root = Path(a.cache)
    todo = [d for d in _weekdays(duka._day(a.start), duka._day(a.end))
            if not dt.candle_path(root, a.symbol, d).exists()]
    print(f"{len(todo)} weekday files to fetch into {root} ({a.pause}s pause)", flush=True)
    got = empty = 0
    failed: list = []
    limited = 0
    t0 = time.time()
    for i, day in enumerate(todo):
        body: Optional[bytes] = None
        for attempt in range(6):
            try:
                body = duka.fetch(dt.candle_url(a.symbol, day))
                limited = 0
                break
            except hb.RateLimited as rl:
                limited += 1
                if limited > 5:
                    print("STOPPED: rate limited five times in a row; rerun to resume.")
                    return 1
                time.sleep(rl.retry_after_s)
            except OSError as exc:
                if attempt == 5:
                    failed.append((day, str(exc)))
                    body = b"FAILED"
                else:
                    time.sleep(2 ** (attempt + 1))
        if body == b"FAILED":
            continue
        p = dt.candle_path(root, a.symbol, day)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".tmp")
        tmp.write_bytes(body or b"")
        tmp.replace(p)
        got, empty = got + bool(body), empty + (not body)
        if (i + 1) % 250 == 0:
            print(f"  {i + 1}/{len(todo)}  {datetime.fromtimestamp(day, timezone.utc):%Y-%m-%d}"
                  f"  fetched {got}, no data {empty}, failed {len(failed)}"
                  f"  ({time.time() - t0:.0f}s)", flush=True)
        time.sleep(a.pause)
    print(f"fetched {got}, no data {empty}, failed {len(failed)} in {time.time() - t0:.0f}s")
    for day, msg in failed[:10]:
        print(f"  FAILED {datetime.fromtimestamp(day, timezone.utc):%Y-%m-%d}: {msg}")
    return 1 if failed else 0


def load_days(root: Path, symbol: str, end: float) -> Iterable[tuple[float, list]]:
    """(day_ts, bars) for every cached weekday file with data, oldest first."""
    sym = root / symbol
    if not sym.exists():
        return
    for p in sorted(sym.glob("*/*/*_BID_m1.bi5")):
        y, m, d = int(p.parent.parent.name), int(p.parent.name), int(p.name[:2])
        day = datetime(y, m, d, tzinfo=timezone.utc).timestamp()
        if day < end and p.stat().st_size:
            yield day, dt.decode_candles(p.read_bytes(), day)


def _stats(trades: list[dict]) -> dict:
    rs = [t["r"] for t in trades]
    n = len(rs)
    if n < 3:
        return {}
    m = sum(rs) / n
    sd = math.sqrt(sum((r - m) ** 2 for r in rs) / (n - 1))
    if sd == 0:
        return {}
    skew = sum(((r - m) / sd) ** 3 for r in rs) / n
    kurt = sum(((r - m) / sd) ** 4 for r in rs) / n
    sr = m / sd
    return {"sharpe_per_trade": sr,
            "deflated_sharpe": deflated_sharpe(sr, n, dt.N_TRIALS, skew, kurt)}


def run_trials(days: Iterable[tuple[float, list]], cost: float,
               say: Callable[[str], None] = print) -> dict:
    """The whole run. Returns {"bear_ok": False, ...} without computing a
    single trial if the bear window does not hold."""
    d1, d2, d1_raw, d2_raw = [], [], [], []
    rows: list = []
    drop = Counter()
    mean_px: dict = {}
    for day, bars in days:
        one = {day: bars}
        t, n = dt.asian_long(one, cost)
        d1 += t
        drop["D1"] += n
        t, n = dt.friday_long(one, cost)
        d2 += t
        drop["D2"] += n
        r, n = dt.daily_bars(one)
        rows += r
        drop["D3"] += n
    for day, _o, c in rows:
        mean_px.setdefault(datetime.fromtimestamp(day, timezone.utc).year, []).append(c)

    ok, closes = dt.bear_window_ok(rows)
    say(f"Data: {len(rows)} trading days, {min(mean_px) if mean_px else '-'}-"
         f"{max(mean_px) if mean_px else '-'}. Dropped: D1 {drop['D1']}, D2 {drop['D2']}, "
         f"D3 {drop['D3']}.")
    say("Bear window (each must close below the year before): " + ", ".join(
        f"{y} {closes.get(y - 1, float('nan')):.2f} -> {closes.get(y, float('nan')):.2f}"
        for y in dt.BEAR_YEARS) + f"  => {'HOLDS' if ok else 'DOES NOT HOLD'}")
    if not ok:
        say("Stopping before any trial: correct the bear window in the spec first.")
        return {"bear_ok": False, "closes": closes}

    d3 = dt.sma200_trades(rows, cost)
    trials = {"D1": d1, "D2": d2, "D3": d3, "D4": dt.asian_above_sma(d1, d3)}
    before = {"D1": cost, "D2": cost, "D3": cost, "D4": cost}
    px = {y: sum(v) / len(v) for y, v in mean_px.items()}
    out: dict = {"bear_ok": True, "closes": closes, "dropped": dict(drop), "trials": {}}
    say(f"\nBar: pooled t >= {dt.T_BAR:.2f}; positive in > 2/3 of years with >= "
         f"{dt.MIN_YEAR_N} trades; > 0 in {dt.BEAR_YEARS[0]}-{dt.BEAR_YEARS[-1]}, "
         f"pre-{dt.SPLIT_YEAR} and {dt.SPLIT_YEAR}+. Cost {cost} points a trade.\n")
    for key, trades in trials.items():
        v = dt.verdict(trades)
        v.update(_stats(trades))
        if key == "D4":
            d1_mean = out["trials"]["D1"]["mean"]
            if not v["mean"] > d1_mean:
                v["confirmed"] = False
                v["why"].append(f"mean {v['mean']:+.3f} does not beat D1's {d1_mean:+.3f}")
        out["trials"][key] = v
        t = v["t"] if v["t"] is not None else float("nan")
        say(f"{key} {NAMES[key]}: n={v['n']}  mean {v['mean']:+.3f} pts "
             f"(before cost {v['mean'] + before[key]:+.3f})  t {t:+.2f}  "
             f"DSR {v.get('deflated_sharpe', float('nan')):.3f}  -> "
             + ("CONFIRMED" if v["confirmed"] else "no edge (" + "; ".join(v["why"]) + ")"))
        say(f"    bear {v['bear']:+.3f}   pre-{dt.SPLIT_YEAR} {v['pre']:+.3f}   "
             f"{dt.SPLIT_YEAR}+ {v['post']:+.3f}   positive years "
             f"{len(v['positive_years'])}/{len(v['voting_years'])}")
        for y, m in v["year_mean"].items():
            pct = 100 * m / px[y] if y in px else float("nan")
            tag = "" if y in v["voting_years"] else "  (under 30, not voting)"
            say(f"    {y}  n={v['year_n'][y]:4d}  mean {m:+8.3f} pts  {pct:+.4f}%{tag}")
    return out


def cmd_run(a) -> int:
    t0 = time.time()
    out = run_trials(load_days(Path(a.cache), a.symbol, END), a.cost)
    print(f"\n({time.time() - t0:.0f}s)")
    if a.out:
        Path(a.out).write_text(json.dumps(out, default=str, indent=1))
        print(f"results written to {a.out}")
    return 0 if out["bear_ok"] else 2


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("download")
    p.add_argument("--start", required=True, help="YYYY-MM-DD, inclusive")
    p.add_argument("--end", required=True, help="YYYY-MM-DD, exclusive")
    p.add_argument("--pause", type=float, default=1.0)
    r = sub.add_parser("run")
    r.add_argument("--cost", type=float, default=COST)
    r.add_argument("--out", default="")
    for q in (p, r):
        q.add_argument("--cache", default=str(DEFAULT_CACHE))
        q.add_argument("--symbol", default=SYMBOL)
    a = ap.parse_args()
    return cmd_download(a) if a.cmd == "download" else cmd_run(a)


if __name__ == "__main__":
    sys.exit(main())
