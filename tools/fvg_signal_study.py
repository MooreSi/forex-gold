#!/usr/bin/env python3
"""Measure whether the reference channel's signals actually line up with Fair
Value Gaps, and whether FVG confluence predicts a better outcome.

WHY
---
Their own chart screenshots run TradingView's FVG/iFVG indicator, so the
working assumption is that they set up from imbalances. That is an
assumption until it is measured. This answers two separate questions:

  1. DESCRIPTIVE: how often does a signal's entry actually sit in, or near,
     an FVG? If it does not, the whole premise is wrong and the new ML
     features are noise.
  2. PREDICTIVE: among signals that DID trade, did FVG confluence coincide
     with better realised R? That is what justifies the ml_engine v6
     features carrying any weight.

Deliberately reports its own data coverage. The MT5 bridge only serves a
recent candle window, so signals older than that cannot be evaluated at
all, and a study that quietly measured 12 signals while implying it
measured 800 would be worse than no study.

Usage:  tools/fvg_signal_study.py [--days N] [--tf M15]
"""
from __future__ import annotations

import argparse
import json
import statistics
import subprocess
import sys
import time
from collections import defaultdict

sys.path.insert(0, "/Users/simon/Documents/FOREX.nosync")

BRIDGE = "http://localhost:9010"
DATA = "/Users/simon/Library/Application Support/ForexTrader/data"
DB = f"{DATA}/forex_trader_demo.db"


def fetch_candles(frm: float, to: float, tf: str) -> list[dict]:
    try:
        out = subprocess.run(
            ["curl", "-s", "--max-time", "20",
             f"{BRIDGE}/candles_range?from={frm}&to={to}&timeframe={tf}"],
            capture_output=True, text=True,
        ).stdout
        return json.loads(out).get("candles", []) or []
    except Exception:
        return []


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--tf", default="M15")
    args = ap.parse_args()

    import sqlite3
    from forex_trader.reversal_engine.ict_patterns import fvg_context, detect_fvgs

    since = time.time() - args.days * 86400
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sigs = [dict(r) for r in con.execute(
        "SELECT s.signal_id, s.source_name, s.direction, s.entry_low, s.entry_high, "
        "       s.created_at, t.net_pnl, t.status, t.mt5_ticket "
        "FROM vantage_signals s "
        "LEFT JOIN vantage_simulated_trades t ON t.signal_id = s.signal_id "
        "WHERE s.created_at > ? AND s.source_name LIKE '%Gold Diggers%' "
        "ORDER BY s.created_at", (since,),
    ).fetchall()]
    con.close()

    print(f"Gold Diggers signals in last {args.days}d: {len(sigs)}")
    if not sigs:
        print("Nothing to study.")
        return 0

    # One candle pull covering the whole window, reused for every signal.
    lo = min(s["created_at"] for s in sigs) - 6 * 3600
    hi = max(s["created_at"] for s in sigs) + 3600
    candles = fetch_candles(lo, hi, args.tf)
    print(f"{args.tf} candles available across that window: {len(candles)}")
    if len(candles) < 50:
        print("\nINSUFFICIENT CANDLE HISTORY -- the bridge only serves a recent")
        print("window, so this study cannot evaluate these signals. Re-run with")
        print("a smaller --days, or dump deeper history from MT5 first.")
        return 1

    evaluated, skipped = [], 0
    for s in sigs:
        # Only candles that existed AT signal time -- using later candles
        # would leak the future into the FVG state and invent an edge.
        prior = [c for c in candles if c["ts"] <= s["created_at"]]
        if len(prior) < 30:
            skipped += 1
            continue
        entry = (float(s["entry_low"] or 0) + float(s["entry_high"] or 0)) / 2
        if entry <= 0:
            skipped += 1
            continue
        closes = [c["close"] for c in prior[-15:]]
        atr = max((max(closes) - min(closes)) / 2, 1.0)
        ctx = fvg_context(prior, entry, s["direction"], atr)
        evaluated.append({**s, **ctx, "entry_mid": entry})

    print(f"evaluated: {len(evaluated)}  (skipped for thin history: {skipped})\n")
    if not evaluated:
        return 1

    # 1. DESCRIPTIVE
    inside = [e for e in evaluated if e["fvg_confluence"] >= 1.0]
    partial = [e for e in evaluated if e["fvg_confluence"] == 0.5]
    none_ = [e for e in evaluated if e["fvg_confluence"] == 0.0]
    n = len(evaluated)
    print("=== Do their entries sit in FVGs? ===")
    print(f"  inside an ALIGNED unfilled FVG : {len(inside):4d}  ({100*len(inside)/n:.1f}%)")
    print(f"  inside an opposing FVG         : {len(partial):4d}  ({100*len(partial)/n:.1f}%)")
    print(f"  not inside any FVG             : {len(none_):4d}  ({100*len(none_)/n:.1f}%)")
    dists = [e["fvg_dist_norm"] for e in evaluated]
    print(f"  median distance to nearest aligned FVG: {statistics.median(dists):.2f} ATR")

    # 2. PREDICTIVE -- only signals that actually traded have an outcome
    traded = [e for e in evaluated if e.get("mt5_ticket") and e.get("net_pnl") is not None]
    print(f"\n=== Did confluence predict outcome? (traded signals: {len(traded)}) ===")
    if len(traded) < 10:
        print("  Too few traded signals to say anything. Re-run once more have closed.")
        return 0
    buckets = defaultdict(list)
    for e in traded:
        key = ("in aligned FVG" if e["fvg_confluence"] >= 1.0
               else "in opposing FVG" if e["fvg_confluence"] == 0.5 else "no FVG")
        buckets[key].append(float(e["net_pnl"]))
    print(f"  {'bucket':<18} {'n':>4} {'win%':>7} {'avg $':>9} {'total $':>10}")
    for key, pnls in sorted(buckets.items()):
        wins = sum(1 for p in pnls if p > 0)
        print(f"  {key:<18} {len(pnls):>4} {100*wins/len(pnls):>6.1f}% "
              f"{statistics.mean(pnls):>9.2f} {sum(pnls):>10.2f}")
    print("\n  Treat small buckets with suspicion; this is a description of what")
    print("  happened, not a validated edge.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
