"""Exit-policy lab -- find stop/target rules that fix the risk:reward
problem, measured against real historical trade paths.

WHY THIS EXISTS
---------------
tools/risk_replay.py established that position SIZING dominates recent
P&L swings. Fixing sizing caps the damage but cannot make a break-even
system profitable: per-strategy expectancy measured ~0 (+0.008 to
+0.043R). This tool attacks the other half -- the payoff structure.

THE DIAGNOSIS
-------------
Reconstructing each closed position's M1 path (2026-07-26..28, 67 trades)
gives maximum favourable/adverse excursion per trade:

                n   MFE     captured  MAE    stop
  winners      39   5.21pt  2.53      2.04   8.21
  losers       13   1.53pt  -6.22     8.55   6.93
  all          52   4.29pt  0.35      3.67   7.89

Two things jump out:

1. Stops are wider (7.89) than the average best-case move (4.29). Even a
   perfect exit caps out near +0.54R. The geometry is upside-down.
2. Winners only go 2.04pt against you before working; losers go 8.55.
   That separation is the exploitable signal -- a stop near 4pt keeps
   most winners while cutting losers roughly in half.

WHAT THE SWEEP FOUND
--------------------
Expectancy is monotone in "tighter stop, wider target" across the whole
grid -- a structural gradient, not one lucky cell:

  s8/t4 (roughly current):  +0.097R      s4/t6:  +0.418R
  s5/t8:                    +0.475R      s3/t8:  +0.697R

Breakeven moves REDUCE expectancy in every single cell tested (8/8, both
halves). Moving to breakeven at +2pt costs 0.10-0.36R depending on
config. This is the mechanism behind the observed payoff shape: 64% of
trades had their stop moved to BE and ~16% closed at exactly $0.00 --
would-be winners converted into scratches while losers still paid full
freight. The channels' own "RISK FREE" instructions drive this, and the
app implements them faithfully; the data says they are costly.

VALIDATION (do not skip when re-running)
----------------------------------------
- Chronological holdout: config ranking identical in both halves.
  s4/t6 = +0.439 / +0.397; s8/t4 = +0.091 / +0.103.
- Bootstrap 95% CI (2000 resamples): s4/t6 = +0.418R [+0.119, +0.716] --
  entirely above zero. Current s8/t4 = +0.097R [-0.060, +0.254] --
  straddles zero, i.e. indistinguishable from no edge.
- Spread modelled explicitly (cost = spread/stop in R): tighter stops pay
  proportionally more, and the ranking still holds. s4/t6 nets +0.358R.

HONEST LIMITS
-------------
- 67 trades over 3 days. Small. Re-run as history accumulates.
- Assumes identical ENTRIES. It re-times exits only.
- Intrabar resolution: if a bar touches both stop and target the STOP is
  assumed first, biasing results pessimistic.
- Slippage beyond spread is not modelled; a 3pt stop is more exposed to
  it than an 8pt stop, which is why s4 is recommended over s3 despite s3
  scoring higher.

Usage:
    .venv/bin/python tools/exit_policy_lab.py
    .venv/bin/python tools/exit_policy_lab.py --spread 0.30 --hours 4
"""
from __future__ import annotations

import argparse
import collections
import json
import random
import sqlite3
import sys
import urllib.request

DB = ("/Users/simon/Library/Application Support/ForexTrader/"
      "data/forex_trader_demo.db")
BRIDGE = "http://localhost:9010"
MAX_PLAUSIBLE_STOP = 100.0


def load_trades(hours: int):
    cd = json.load(urllib.request.urlopen(
        f"{BRIDGE}/candles/XAUUSD?timeframe=M1&count=3000"))["candles"]
    hist = json.load(urllib.request.urlopen(f"{BRIDGE}/history?days=3"))["history"]
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    rows = {r["mt5_ticket"]: r for r in con.execute(
        "SELECT mt5_ticket,strategy,sl_moved_to_be,entry_price,stop_loss "
        "FROM vantage_simulated_trades WHERE status='closed' AND mt5_ticket>0")}

    pos = collections.defaultdict(lambda: {"in": None, "outs": []})
    for d in hist:
        if d.get("entry") == 0:
            pos[d["position_id"]]["in"] = d
        elif d.get("entry") == 1:
            pos[d["position_id"]]["outs"].append(d)

    cd.sort(key=lambda c: c["ts"])
    out = []
    for pid, v in pos.items():
        if not v["in"] or not v["outs"] or pid not in rows:
            continue
        ind = v["in"]
        entry = ind["price"]
        if not entry:
            continue
        t0 = ind["time"]
        # Same horizon for every policy, so a wider target isn't unfairly
        # truncated by whenever the OLD rule happened to close the trade.
        seg = [c for c in cd if t0 <= c["ts"] <= t0 + hours * 3600]
        if len(seg) < 5:
            continue
        out.append({"entry": entry, "isbuy": ind["type"] == 0, "seg": seg,
                    "strat": rows[pid]["strategy"], "t0": t0})
    out.sort(key=lambda t: t["t0"])
    return out


def simulate(t, stop_pts, target_pts, be_at=None) -> float:
    """R multiple for one trade under a candidate rule."""
    e, isbuy = t["entry"], t["isbuy"]
    stop = e - stop_pts if isbuy else e + stop_pts
    tgt = e + target_pts if isbuy else e - target_pts
    moved = False
    for c in t["seg"]:
        hi, lo = c["high"], c["low"]
        if isbuy:
            if lo <= stop:
                return 0.0 if moved else -1.0
            if be_at and not moved and hi - e >= be_at:
                stop, moved = e, True
            if hi >= tgt:
                return target_pts / stop_pts
        else:
            if hi >= stop:
                return 0.0 if moved else -1.0
            if be_at and not moved and e - lo >= be_at:
                stop, moved = e, True
            if lo <= tgt:
                return target_pts / stop_pts
    last = t["seg"][-1]["close"]
    move = (last - e) if isbuy else (e - last)
    if moved and move < 0:
        return 0.0
    return move / stop_pts


def expectancy(trades, s, tg, be=None) -> float:
    rs = [simulate(t, s, tg, be) for t in trades]
    return sum(rs) / len(rs)


def bootstrap_ci(trades, s, tg, be=None, n=2000):
    rs = [simulate(t, s, tg, be) for t in trades]
    boots = sorted(sum(random.choice(rs) for _ in rs) / len(rs) for _ in range(n))
    return sum(rs) / len(rs), boots[int(0.025 * n)], boots[int(0.975 * n)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spread", type=float, default=0.24)
    ap.add_argument("--hours", type=int, default=4)
    ap.add_argument("--risk", type=float, default=4.60,
                    help="dollars risked per trade, for the $ column")
    args = ap.parse_args()

    trades = load_trades(args.hours)
    if len(trades) < 20:
        print(f"Only {len(trades)} trades with M1 paths -- too few to trust.")
        return 1
    print(f"{len(trades)} trades with usable M1 paths, {args.hours}h horizon, "
          f"spread {args.spread}\n")

    stops = [2, 3, 4, 5, 6, 8, 10]
    targets = [2, 3, 4, 5, 6, 8]

    for label, be in (("no breakeven move", None), ("breakeven at +2pt", 2.0)):
        print(f"=== expectancy (R, gross) -- {label} ===")
        print("         " + "".join(f"tgt{t:<6}" for t in targets))
        for s in stops:
            line = f"  stop {s:<3}"
            for tg in targets:
                line += f"{expectancy(trades, s, tg, be):+6.3f} "
            print(line)
        print()

    print("=== net of spread, ranked ===")
    cand = [(expectancy(trades, s, tg) - args.spread / s, s, tg)
            for s in stops for tg in targets]
    cand.sort(reverse=True)
    print(f"  {'cfg':>10} {'gross':>8} {'spread':>8} {'net R':>8} {'$/trade':>9}")
    for net, s, tg in cand[:8]:
        g = expectancy(trades, s, tg)
        print(f"  {'s'+str(s)+'/t'+str(tg):>10} {g:+8.3f} {args.spread/s:8.3f} "
              f"{net:+8.3f} {net*args.risk:+9.2f}")

    half = len(trades) // 2
    A, B = trades[:half], trades[half:]
    print(f"\n=== chronological holdout (n={len(A)} / n={len(B)}) ===")
    print(f"  {'cfg':>12} {'1st':>8} {'2nd':>8} {'both':>8}")
    for s, tg in [(3, 8), (4, 8), (4, 6), (5, 8), (5, 5), (8, 4)]:
        print(f"  {'s'+str(s)+'/t'+str(tg):>12} {expectancy(A,s,tg):+8.3f} "
              f"{expectancy(B,s,tg):+8.3f} {expectancy(trades,s,tg):+8.3f}")

    print("\n=== breakeven-move penalty (negative = BE costs money) ===")
    for s, tg in [(3, 6), (4, 6), (5, 8), (8, 4)]:
        d = expectancy(trades, s, tg, 2.0) - expectancy(trades, s, tg)
        print(f"  s{s}/t{tg}: {d:+.3f}R")

    print("\n=== bootstrap 95% CI ===")
    for s, tg in [(4, 6), (5, 8), (8, 4)]:
        m, lo, hi = bootstrap_ci(trades, s, tg)
        verdict = "above zero" if lo > 0 else "STRADDLES ZERO"
        print(f"  s{s}/t{tg}: {m:+.3f}R  [{lo:+.3f}, {hi:+.3f}]  {verdict}")

    print("\nRecommended: stop 4 / target 6, no breakeven move.\n"
          "s3/t8 scores higher but a 3pt stop is far more exposed to\n"
          "slippage and spread widening, which this model does not capture.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
