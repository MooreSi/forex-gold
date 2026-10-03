#!/usr/bin/env python3
"""Amendment A of docs/todo/reversal-engine/270: the three holdout trials on
Dukascopy 2019-2024, judged once against the bar registered before these
years were read.

    A1  NY opening-range breakout with the H4 trend (production orb_ny)
    A2  Trend PA, 12:00-20:00 UTC (production trend_pa.backtest)
    A3  Intraday momentum REVERSED: fade the COMEX first half-hour

Research only: reads the Dukascopy cache, nothing else. No orders.

Usage:
    .venv/bin/python -m tools.edge_holdout [--cache DIR] [--out results.json]
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from backend.src.services.backtest import edge_trials as et
from backend.src.services.reversal_engine import historical_bars as hb
from backend.src.services.trend_pa import backtest as tpa
from tools import re_dukascopy_replay as duka
from tools.edge_trials_2025 import COST, COST_013

START = datetime(2019, 1, 1, tzinfo=timezone.utc).timestamp()
END = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", default=str(duka.DEFAULT_CACHE))
    ap.add_argument("--out", default="")
    a = ap.parse_args()

    t0 = time.time()
    m1 = hb.load_m1(Path(a.cache), duka.SYMBOL, START, END)
    print(f"2019-2024: {len(m1)} M1 bars over {len({int(b.ts // 3600) for b in m1})} "
          f"trading hours (loaded in {time.time() - t0:.0f}s)", flush=True)
    m5, m15 = hb.aggregate(m1, 300), hb.aggregate(m1, 900)
    h1, h4 = hb.aggregate(m1, 3600), hb.aggregate(m1, 14400)

    trials = {}
    t0 = time.time()
    trials["A1 NY ORB with H4 trend (production)"] = et.orb_ny_trades(m5, h4, True, COST_013)
    print(f"A1 done ({time.time() - t0:.0f}s)", flush=True)
    t0 = time.time()
    tr = tpa.run(et.as_dicts(h4), et.as_dicts(h1), et.as_dicts(m15),
                 params={"session_start_utc": 12, "session_end_utc": 20},
                 cost=COST_013, offset_s=0)
    trials["A2 Trend PA 12-20 UTC (production)"] = [
        {"ts": x["created_at"], "side": 1 if x["direction"] == "BUY" else -1, "r": x["r_net"]}
        for x in tr]
    print(f"A2 done ({time.time() - t0:.0f}s)", flush=True)
    trials["A3 COMEX first half-hour, faded (points)"] = et.reverse_points(
        et.intraday_momentum(m1, COST), COST)

    out = {}
    print(f"\nBar: pooled t >= {et.HOLDOUT_T_BAR:.2f} (Bonferroni for "
          f"{et.HOLDOUT_TRIALS}), positive in more than two thirds of years with "
          f">= {et.HOLDOUT_MIN_YEAR_N} trades.\n")
    for name, trades in trials.items():
        j = et.holdout_judge(trades)
        out[name] = j
        t = j["t"] if j["t"] is not None else float("nan")
        print(f"{name}: n={j['n']}  mean {j['mean']:+.3f}  t {t:+.2f}  -> "
              f"{'PASSES' if j['passes'] else 'fails'}"
              + ("" if j["passes"] else "  (" + "; ".join(j["why"]) + ")"))
        for y, v in j["years"].items():
            tag = "" if y in j["counted_years"] else "  (under 30, not counted)"
            print(f"    {y}  n={v['n']:4d}  mean {v['mean']:+.3f}{tag}")
    if a.out:
        Path(a.out).write_text(json.dumps(out, default=str, indent=1))
        print(f"\nresults written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
