#!/usr/bin/env python3
"""Replay every Reversal Engine signal on M1 history and ask whether any
entry rule, or any fact about the approach, beats a placebo entry.

docs/todo/reversal-engine/240, phase 1. The arithmetic lives in
`backend/src/services/reversal_engine/entry_study.py` and is tested there;
this file fetches, caches and prints.

READ-ONLY. It reads `reversal_engine.db` and the demo database through
`mode=ro` URIs and fetches candles from the bridge's `/candles_range`.
Nothing here places, modifies or closes an order.

Usage:
    .venv/bin/python -m tools.re_entry_study [--cache DIR] [--cost 0.575]
                                             [--out rows.json] [--since YYYY-MM-DD]

The verdict bar (240): z >= 3 against the placebo with trades clustered by
2-hour block, n >= 200, and positive mean R after costs in BOTH halves of
history.
"""
from __future__ import annotations

import argparse
import bisect
import json
import math
import random
import sqlite3
import statistics
import sys
import time
import urllib.request
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from backend.src.config import DATA_DIR
from backend.src.services.reversal_engine import entry_study as es

BRIDGE = "http://localhost:9010"
# Measured 2026-09-24 in execution_quality for this template: spread 0.219
# plus slippage 0.357 over 446 trades.
DEFAULT_COST = 0.575
CLUSTER_S = 7200


def _ro(path: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def load_signals(since: float) -> list[dict]:
    with _ro(DATA_DIR / "reversal_engine.db") as c:
        return [dict(r) for r in c.execute(
            "SELECT id, created_at, direction, entry_low, entry_high, level_price,"
            " level_type, session, status, outcome, live_exec_status, mt5_ticket,"
            " ml_prob FROM re_signals WHERE created_at >= ? AND entry_low > 0"
            " AND entry_high > 0 AND level_price > 0 ORDER BY created_at",
            (since,))]


def load_real_results() -> dict:
    """mt5_ticket -> R on the template's 5-point stop, from the demo DB."""
    out = {}
    path = DATA_DIR / "forex_trader_demo.db"
    if not path.exists():
        return out
    with _ro(path) as c:
        for r in c.execute(
                "SELECT mt5_ticket, mt5_profit, lot_size FROM vantage_simulated_trades"
                " WHERE status='closed' AND mt5_ticket IS NOT NULL"
                " AND mt5_profit IS NOT NULL AND lot_size > 0"):
            out[int(r["mt5_ticket"])] = float(r["mt5_profit"]) / (float(r["lot_size"]) * 100 * 5.0)
    return out


def fetch_day(day_start: int, cache: Path) -> list[dict]:
    f = cache / f"m1_{day_start}.json"
    if f.exists():
        return json.loads(f.read_text())
    url = (f"{BRIDGE}/candles_range?from={day_start}&to={day_start + 86_400}"
           f"&timeframe=M1")
    with urllib.request.urlopen(url, timeout=60) as resp:
        rows = json.loads(resp.read()).get("candles", [])
    # Today's file would freeze a partial day; cache only finished days.
    if day_start + 86_400 < time.time() - 3600:
        f.write_text(json.dumps(rows))
    return rows


def load_bars(signals: list[dict], cache: Path) -> list[es.Bar]:
    days = set()
    for s in signals:
        d0 = int(s["created_at"] // 86_400) * 86_400
        for k in (-1, 0, 1):
            days.add(d0 + k * 86_400)
    rows = []
    for d in sorted(days):
        if d > time.time():
            continue
        rows.extend(fetch_day(d, cache))
    bars = es.bars_from(rows)
    dedup, seen = [], set()
    for b in bars:
        if b.ts not in seen:
            seen.add(b.ts)
            dedup.append(b)
    return dedup


def window(bars, ts_index, t_from, t_to):
    i = bisect.bisect_left(ts_index, t_from)
    j = bisect.bisect_right(ts_index, t_to)
    return bars[i:j]


def study_signal(s, bars, ts_index, cost, rng):
    sig = es.Sig(int(s["id"]), float(s["created_at"]), str(s["direction"]).upper(),
                 float(s["entry_low"]), float(s["entry_high"]),
                 float(s["level_price"]), s["level_type"] or "", s["session"] or "")
    seg = window(bars, ts_index, sig.created_at - 26 * 3600,
                 sig.created_at + es.MAX_WAIT_S + es.HORIZON_S + 13 * 3600)
    touch = es.find_touch(seg, sig)
    if touch is None:
        return None
    feats = es.features(seg, touch, sig)
    if feats is None:
        return None
    row = {"id": sig.id, "ts": touch.ts, "direction": sig.direction,
           "level_type": sig.level_type, "session": sig.session,
           "cluster": int(touch.ts // CLUSTER_S),
           "executed": s["live_exec_status"] == "executed",
           "mt5_ticket": s["mt5_ticket"], "ml_prob": s["ml_prob"], **feats}

    tpl = es.replay(seg, touch, sig.direction, es.template_policy(cost))
    row["tpl_r"] = tpl.r_multiple if tpl else None
    for name, (stop, tgt) in {"b55": (5.0, 5.0), "b54": (5.0, 4.0)}.items():
        pol = es.barrier_policy(stop, tgt, cost)
        res = es.replay(seg, touch, sig.direction, pol)
        row[f"{name}_won"] = es.won(res)
        row[f"{name}_r"] = res.r_multiple if res else None
        row[f"{name}_p"] = es.placebo_rate(seg, touch, sig.direction,
                                           es.barrier_policy(stop, tgt, 0.0), rng)

    conf = es.find_confirmation(seg, touch, sig)
    if conf is not None:
        centry, cstop = conf
        row["conf_stop"] = cstop
        row["conf_ts"] = centry.ts
        for mult in (1.0, 2.0):
            pol = es.barrier_policy(cstop, cstop * mult, cost)
            res = es.replay(seg, centry, sig.direction, pol)
            k = f"c{int(mult)}"
            row[f"{k}_won"] = es.won(res)
            row[f"{k}_r"] = res.r_multiple if res else None
            row[f"{k}_p"] = es.placebo_rate(seg, centry, sig.direction,
                                            es.barrier_policy(cstop, cstop * mult, 0.0), rng)
    return row


# ── Reporting ──────────────────────────────────────────────────────────────

def verdict(rows, key, halves_split):
    rs = [r for r in rows if r.get(f"{key}_won") is not None and r.get(f"{key}_p") is not None]
    if not rs:
        return None
    b = es.beats_placebo([r[f"{key}_won"] for r in rs], [r[f"{key}_p"] for r in rs],
                         [r["cluster"] for r in rs])
    rr = [r[f"{key}_r"] for r in rs if r.get(f"{key}_r") is not None]
    h1 = [r[f"{key}_r"] for r in rs if r["ts"] < halves_split and r.get(f"{key}_r") is not None]
    h2 = [r[f"{key}_r"] for r in rs if r["ts"] >= halves_split and r.get(f"{key}_r") is not None]
    mean = lambda xs: (sum(xs) / len(xs)) if xs else float("nan")
    b.update(mean_r=mean(rr), r_h1=mean(h1), r_h2=mean(h2))
    b["passes"] = bool(b["z"] is not None and b["z"] >= 3 and b["n"] >= 200
                       and b["r_h1"] > 0 and b["r_h2"] > 0)
    return b


def fmt(name, b):
    if not b or b.get("win_rate") is None:
        return f"  {name:32s} (no rows)"
    return (f"  {name:32s} n={b['n']:5d} cl={b['clusters']:4d} "
            f"win={b['win_rate'] * 100:5.1f}% placebo={b['placebo'] * 100:5.1f}% "
            f"z={b['z']:+5.2f}  R={b['mean_r']:+.3f} (h1 {b['r_h1']:+.3f} / h2 {b['r_h2']:+.3f})"
            f"{'  PASSES' if b['passes'] else ''}")


def report(rows, real):
    ts_sorted = sorted(r["ts"] for r in rows)
    split = ts_sorted[len(ts_sorted) // 2]
    print(f"\nSignals replayed: {len(rows)}  "
          f"({datetime.fromtimestamp(ts_sorted[0], timezone.utc):%Y-%m-%d} to "
          f"{datetime.fromtimestamp(ts_sorted[-1], timezone.utc):%Y-%m-%d}, "
          f"halves split {datetime.fromtimestamp(split, timezone.utc):%Y-%m-%d %H:%M})")

    print("\n1. Is the replay faithful? Executed trades, replay vs the broker")
    pairs = [(r["tpl_r"], real[int(r["mt5_ticket"])]) for r in rows
             if r["executed"] and r["mt5_ticket"] and int(r["mt5_ticket"]) in real
             and r["tpl_r"] is not None]
    if len(pairs) >= 10:
        a, b = zip(*pairs)
        agree = sum(1 for x, y in pairs if (x > 0) == (y > 0)) / len(pairs)
        corr = statistics.correlation(a, b) if len(set(a)) > 1 and len(set(b)) > 1 else float("nan")
        print(f"  n={len(pairs)}  mean R replay {statistics.mean(a):+.3f}  real {statistics.mean(b):+.3f}"
              f"  sign agrees {agree * 100:.0f}%  corr {corr:+.2f}")
    else:
        print(f"  only {len(pairs)} executed trades matched; fidelity not measured")

    print("\n2. The touch entry the engine trades (all signals)")
    tpl = [r["tpl_r"] for r in rows if r["tpl_r"] is not None]
    print(f"  template replay mean R after costs: {statistics.mean(tpl):+.3f} over {len(tpl)}")
    print(fmt("stop 5 / target 5", verdict(rows, "b55", split)))
    print(fmt("stop 5 / target 4 (template TP1)", verdict(rows, "b54", split)))

    print("\n3. Groups, stop 5 / target 5 against placebo")
    for key in ("level_type", "session", "direction"):
        for v in sorted({str(r[key]) for r in rows}):
            sub = [r for r in rows if str(r[key]) == v]
            if len(sub) >= 100:
                print(fmt(f"{key}={v}", verdict(sub, "b55", split)))

    print("\n4. Facts about the approach, by quintile (stop 5 / target 5)")
    feats = ["approach_5", "approach_15", "approach_60", "range_ratio_3",
             "volume_ratio_5", "touches_24h", "mins_since_touch", "range_4h",
             "stretch_60", "hour"]
    for f in feats:
        edges = es.quantile_edges([r[f] for r in rows])
        print(f"  {f}  cuts {['%.2f' % e for e in edges]}")
        for q in range(len(edges) + 1):
            sub = [r for r in rows if es.bucket(r[f], edges) == q]
            print("  " + fmt(f"  q{q + 1}", verdict(sub, "b55", split)))

    print("\n5. Confirmation entries (pierce the level, close back, enter next open)")
    conf = [r for r in rows if "c1_won" in r]
    print(f"  confirmed {len(conf)} of {len(rows)} touches; "
          f"median stop {statistics.median([r['conf_stop'] for r in conf]):.2f} pts" if conf else "  none")
    print(fmt("target 1R", verdict(conf, "c1", split)))
    print(fmt("target 2R", verdict(conf, "c2", split)))
    for key in ("level_type", "session"):
        for v in sorted({str(r[key]) for r in conf}):
            sub = [r for r in conf if str(r[key]) == v]
            if len(sub) >= 100:
                print(fmt(f"  1R {key}={v}", verdict(sub, "c1", split)))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--cache", default=str(DATA_DIR / "research_m1_cache"))
    ap.add_argument("--cost", type=float, default=DEFAULT_COST)
    ap.add_argument("--since", default="2026-07-20")
    ap.add_argument("--out", default="")
    ap.add_argument("--seed", type=int, default=240)
    args = ap.parse_args()

    cache = Path(args.cache)
    cache.mkdir(parents=True, exist_ok=True)
    since = datetime.strptime(args.since, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
    signals = load_signals(since)
    print(f"{len(signals)} signals since {args.since}; fetching M1 history...", flush=True)
    bars = load_bars(signals, cache)
    ts_index = [b.ts for b in bars]
    print(f"{len(bars)} M1 bars", flush=True)

    rng = random.Random(args.seed)
    rows = []
    t0 = time.time()
    for k, s in enumerate(signals):
        r = study_signal(s, bars, ts_index, args.cost, rng)
        if r is not None:
            rows.append(r)
        if k % 1000 == 999:
            print(f"  {k + 1}/{len(signals)} ({time.time() - t0:.0f}s)", flush=True)
    if args.out:
        Path(args.out).write_text(json.dumps(rows))
    report(rows, load_real_results())
    return 0


if __name__ == "__main__":
    sys.exit(main())
