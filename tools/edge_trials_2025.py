#!/usr/bin/env python3
"""Run the nine pre-registered edge trials on Dukascopy 2025 and judge them.

docs/todo/reversal-engine/270. Research only: reads the Dukascopy cache that
`tools.re_dukascopy_replay download` filled, and nothing else. No database,
no broker, no orders. Only 2025 is read; 2019-2024 are the holdout.

Usage:
    .venv/bin/python -m tools.edge_trials_2025 [--cache DIR] [--out results.json]
"""
from __future__ import annotations

import argparse
import json
import math
import random
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from statistics import NormalDist

from backend.src.services.backtest import edge_trials as et
from backend.src.services.market import validation
from backend.src.services.reversal_engine import historical_bars as hb
from backend.src.services.reversal_engine import historical_signals as hs
from backend.src.services.reversal_engine import replay_verdict as rv
from backend.src.services.trend_pa import backtest as tpa
from tools import re_dukascopy_replay as duka
from tools import re_entry_study as study

START = datetime(2025, 1, 1, tzinfo=timezone.utc).timestamp()
END = datetime(2026, 1, 1, tzinfo=timezone.utc).timestamp()
COST = study.DEFAULT_COST      # 0.575, measured (240)
COST_013 = 0.30                # what 013's replays charged


def _level_touch(m1, rng):
    gens = [g for g in hs.regenerate(m1) if START <= g.sig.created_at < END]
    ts_index = [b.ts for b in m1]
    rows = []
    for g in gens:
        s = g.sig
        r = study.study_signal(
            {"id": s.id, "created_at": s.created_at, "direction": s.direction,
             "entry_low": s.entry_low, "entry_high": s.entry_high,
             "level_price": s.level_price, "level_type": s.level_type,
             "session": s.session, "live_exec_status": None, "mt5_ticket": None,
             "ml_prob": None}, m1, ts_index, COST, rng)
        if r:
            rows.append(r)
    return len(gens), rows


def _t1(rows):
    s = rv.summarise(rows, "b55")
    h1 = [r["b55_r"] for r in rows if r.get("b55_r") is not None and r["ts"] < et.HALF_SPLIT_2025]
    h2 = [r["b55_r"] for r in rows if r.get("b55_r") is not None and r["ts"] >= et.HALF_SPLIT_2025]
    m1, m2 = (sum(h1) / len(h1) if h1 else float("nan")), (sum(h2) / len(h2) if h2 else float("nan"))
    why = []
    if s["n"] < et.MIN_N:
        why.append(f"n {s['n']} < {et.MIN_N}")
    if not (m1 > 0 and m2 > 0):
        why.append(f"half: {m1:+.3f} / {m2:+.3f} not positive in both")
    if s["z"] is None or s["z"] < et.T_BAR:
        why.append(f"placebo z {s['z']:+.2f} < {et.T_BAR:.2f}")
    rs = [r["b55_r"] for r in rows if r.get("b55_r") is not None]
    mu = sum(rs) / len(rs) if rs else float("nan")
    sd = (sum((x - mu) ** 2 for x in rs) / (len(rs) - 1)) ** 0.5 if len(rs) > 1 else float("nan")
    return {"n": s["n"], "mean": s["mean_r"], "sd": sd, "h1": m1, "h2": m2, "t": s["z"],
            "days": s["clusters"], "candidate": not why, "why": why,
            "note": f"win {s['win_rate'] * 100:.1f}% vs placebo {s['placebo'] * 100:.1f}%"}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", default=str(duka.DEFAULT_CACHE))
    ap.add_argument("--out", default="")
    ap.add_argument("--seed", type=int, default=270)
    a = ap.parse_args()

    t0 = time.time()
    m1 = hb.load_m1(Path(a.cache), duka.SYMBOL, START, END)
    hours = len({int(b.ts // 3600) for b in m1})
    print(f"2025: {len(m1)} M1 bars over {hours} trading hours "
          f"(loaded in {time.time() - t0:.0f}s)", flush=True)
    m5, m15 = hb.aggregate(m1, 300), hb.aggregate(m1, 900)
    h1, h4 = hb.aggregate(m1, 3600), hb.aggregate(m1, 14400)

    results = {}
    t0 = time.time()
    n_sig, rows = _level_touch(m1, random.Random(a.seed))
    results["T1 level-touch, stop 5 / target 5 vs placebo"] = _t1(rows)
    tpl = [{"ts": r["ts"], "r": r["tpl_r"]} for r in rows if r.get("tpl_r") is not None]
    results["T2 level-touch, live template exits"] = et.judge(tpl)
    print(f"T1/T2: {n_sig} signals regenerated, {len(rows)} reached their zone "
          f"({time.time() - t0:.0f}s)", flush=True)

    t3 = et.orb_ny_trades(m5, h4, trend_filter=True, cost=COST_013)
    t4 = et.orb_ny_trades(m5, h4, trend_filter=False, cost=COST_013)
    results["T3 NY ORB with H4 trend (production)"] = et.judge(t3)
    results["T4 NY ORB, no trend filter"] = et.judge(t4)

    d4, d1, d15 = et.as_dicts(h4), et.as_dicts(h1), et.as_dicts(m15)
    for name, params in (("T5 Trend PA 12-20 UTC (production)",
                          {"session_start_utc": 12, "session_end_utc": 20}),
                         ("T6 Trend PA defaults 08-21 UTC", {})):
        tr = tpa.run(d4, d1, d15, params=params, cost=COST_013, offset_s=0)
        results[name] = et.judge([{"ts": x["created_at"], "r": x["r_net"]} for x in tr])

    results["T7 intraday momentum into COMEX settlement (points)"] = \
        et.judge(et.intraday_momentum(m1, COST))
    results["T8 Asian range sweep, faded (R)"] = et.judge(et.asian_sweep_fade(m5, COST))
    results["T9 daily 20-day momentum (points)"] = \
        et.judge(et.daily_momentum(et.daily_bars(m1), 20, COST))

    # Deflated Sharpe (Bailey and Lopez de Prado 2014), for information: the
    # benchmark is the expected best of N no-edge trials SCALED by the spread
    # of the trials' own Sharpes. market/validation.deflated_sharpe assumes
    # unit spread, which with per-trade Sharpes sets an unpassable bar.
    srs = {k: v["mean"] / v["sd"] for k, v in results.items()
           if v["n"] > 2 and v.get("sd") and v["sd"] == v["sd"]}
    mu = sum(srs.values()) / len(srs)
    spread = math.sqrt(sum((x - mu) ** 2 for x in srs.values()) / (len(srs) - 1))
    sr0 = spread * validation.expected_max_sharpe(et.N_TRIALS)
    nd = NormalDist()
    for k, sr in srs.items():
        n = results[k]["n"]
        results[k]["dsr"] = nd.cdf((sr - sr0) * math.sqrt(n - 1) / math.sqrt(1 + 0.5 * sr * sr))

    print("\nTrial                                                   n  clust   mean      H1      H2      t     verdict")
    for k, v in results.items():
        t = v["t"]
        print(f"{k:52s} {v['n']:5d} {v['days']:5d} {v['mean']:+7.3f} {v['h1']:+7.3f} {v['h2']:+7.3f} "
              f"{(t if t is not None else float('nan')):+6.2f}   "
              f"{'CANDIDATE' if v['candidate'] else 'no edge'}"
              + ("" if v["candidate"] else "  (" + "; ".join(v["why"]) + ")")
              + (f"  [{v['note']}]" if v.get("note") else "")
              + (f"  DSR {v['dsr']:.2f}" if "dsr" in v else ""))
    print(f"\nBar: n >= {et.MIN_N}, positive in both halves of 2025, t >= {et.T_BAR:.2f} "
          f"(Bonferroni for {et.N_TRIALS} trials). A candidate is not an edge until "
          f"2019-2024 confirm it.")
    if a.out:
        Path(a.out).write_text(json.dumps(results, default=str, indent=1))
        print(f"results written to {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
