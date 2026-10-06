"""Which template wins for each source in each H1 regime? -- docs/todo/014.

Replays every real signal in the consolidated ledger (source, direction,
open time) through each single-mode EA template's exits on M1 bars, with
`entry_study.replay` and `exit_replay.ExitPolicy` -- the replay that agreed
in sign with 86% of 211 real executed trades (reversal-engine/240). Groups by
source x H1 regime (`read` below) x side (with / against / flat) x
template, and reports mean R separately for the first and second half of the
window, so a cell is only believed when both halves agree.

Reads only. Writes nothing to any database and reaches no broker except for
candle reads when --bars is not given.

Approximations, stated so nobody mistakes the output for a live result:
  * entry is the open of the minute the trade opened in (the ledger has no
    fill price);
  * a template's ladder is folded into one partial at TP1 (tp1_pct) and a
    runner to its last TP, breakeven at TP1 when be_trigger is 1, trail from
    its own trail fields -- the same folding `entry_study.template_policy`
    uses;
  * grid templates are skipped (their legs are not a single entry);
  * round-trip cost 0.575 points (execution_quality, 2026-09-24).

Usage:
    python -m tools.auto_template_study --db <consolidated db> --bars m1.json \
        [--since 2026-08-20] [--hours 6] [--threshold 0.4] [--json out.json]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from dataclasses import dataclass
from typing import Optional, Sequence

from backend.src.services.market.exit_replay import ExitPolicy
from backend.src.services.reversal_engine import entry_study as es

COST_PTS = 0.575
MIN_N = 20


# ── H1 regime: trend or range by efficiency ratio ─────────────────────────
# Lives here, not in services/, because nothing in the app uses it: the
# study found no regime cell worth wiring into Auto (docs/todo/014, Results).
# Why this measure and not dpm_engine.detect_regime: on 2026-10-05 (choppy)
# and 2026-10-06 (a rally) that classifier said "trending" ~60% of both days;
# the efficiency ratio over the day was 0.05 against 0.21.
@dataclass(frozen=True)
class RegimeRead:
    regime: str      # "trend" | "range"
    direction: str   # "up" | "down" | "flat"
    er: float


def read(bars: Sequence[dict], hours: int, threshold: float) -> Optional[RegimeRead]:
    """The regime over the last `hours` closed H1 bars, or None with fewer."""
    if hours <= 0 or len(bars) < hours:
        return None
    window = bars[-hours:]
    net = float(window[-1]["close"]) - float(window[0]["open"])
    path = sum(abs(float(b["close"]) - float(b["open"])) for b in window)
    er = round(abs(net) / path, 4) if path > 0 else 0.0
    direction = "up" if net > 0 else "down" if net < 0 else "flat"
    regime = "trend" if er >= threshold and direction != "flat" else "range"
    return RegimeRead(regime, direction, er)


def side(r: Optional[RegimeRead], trade_direction: str) -> str:
    """"with" / "against" the trend, or "flat" in a range or when unknown."""
    if r is None or r.regime != "trend":
        return "flat"
    buy = str(trade_direction).upper() == "BUY"
    return "with" if (r.direction == "up") == buy else "against"


def canon_source(src: str) -> str:
    s = (src or "").strip()
    for pre in ("Telegram Auto (", "instant:"):
        if s.startswith(pre):
            s = s[len(pre):]
    return s.rstrip(")").strip() or "-"


def policy_for(t: dict) -> Optional[ExitPolicy]:
    """A single-mode template as one partial plus a runner, in points."""
    if (t.get("mode") or "single") != "single" or not t.get("sl_pips"):
        return None
    tps = [(float(t.get(f"tp{i}_pips") or 0), float(t.get(f"tp{i}_pct") or 0))
           for i in range(1, 9)]
    tps = [(p, pct) for p, pct in tps if p > 0]
    if not tps:
        return None
    tp1, pct1 = tps[0]
    last = tps[-1][0]
    frac = min(max(pct1 / 100.0, 0.0), 1.0) if len(tps) > 1 else 1.0
    be = tp1 / 10 if int(t.get("be_trigger") or 0) == 1 else 0.0
    return ExitPolicy(
        stop_pts=float(t["sl_pips"]) / 10, target_pts=tp1 / 10, target_frac=frac or 1.0,
        runner_target_pts=last / 10 if len(tps) > 1 else 0.0,
        be_trigger_pts=be, be_buffer_pts=float(t.get("be_buffer_pts") or 0) / 10 if be else 0.0,
        trail_activation_pts=float(t.get("trail_activation") or 0) / 10,
        trail_distance_pts=float(t.get("trail_distance") or 0) / 10,
        trail_step_pts=float(t.get("trail_step") or 0) / 10,
        cost_pts=COST_PTS)


def h1_from_m1(bars: list[es.Bar]) -> list[dict]:
    out: dict[int, dict] = {}
    for b in bars:
        h = int(b.ts // 3600) * 3600
        c = out.get(h)
        if c is None:
            out[h] = {"ts": float(h), "open": b.open, "high": b.high, "low": b.low, "close": b.close}
        else:
            c["high"] = max(c["high"], b.high)
            c["low"] = min(c["low"], b.low)
            c["close"] = b.close
    return [out[k] for k in sorted(out)]


def closed_h1_before(h1: list[dict], ts: float) -> list[dict]:
    lo, hi = 0, len(h1)
    while lo < hi:                     # first bar whose hour has not closed by ts
        mid = (lo + hi) // 2
        if h1[mid]["ts"] + 3600 <= ts:
            lo = mid + 1
        else:
            hi = mid
    return h1[:lo]


def bar_index(bars: list[es.Bar], ts: float) -> Optional[int]:
    lo, hi = 0, len(bars)
    while lo < hi:
        mid = (lo + hi) // 2
        if bars[mid].ts <= ts:
            lo = mid + 1
        else:
            hi = mid
    i = lo - 1
    return i if i >= 0 and ts - bars[i].ts < es.BAR_S else None


def signals(db_path: str, since_ts: float) -> list[tuple[float, str, str]]:
    con = sqlite3.connect(db_path)
    rows = con.execute(
        "SELECT MIN(open_time), direction, tg_source FROM consolidated_trades "
        "WHERE engine='main' AND open_time >= ? AND mt5_ticket IS NOT NULL "
        "AND mt5_ticket NOT IN ('', '0') GROUP BY mt5_ticket ORDER BY 1", (since_ts,)).fetchall()
    con.close()
    return [(float(t), str(d).upper(), canon_source(s)) for t, d, s in rows if t and d]


def templates(db_path: str) -> dict[str, ExitPolicy]:
    con = sqlite3.connect(db_path)
    con.row_factory = sqlite3.Row
    out = {}
    for r in con.execute("SELECT * FROM ea_trade_templates"):
        p = policy_for(dict(r))
        if p is not None:
            out[r["name"]] = p
    con.close()
    return out


def study(sigs, bars, h1, tpls, hours, threshold):
    """{(source, regime, side, template): {"a": [R...], "b": [R...]}}"""
    if not sigs:
        return {}
    split = sigs[len(sigs) // 2][0]
    cells: dict = defaultdict(lambda: {"a": [], "b": []})
    for ts, direction, src in sigs:
        idx = bar_index(bars, ts)
        if idx is None:
            continue
        r = read(closed_h1_before(h1, ts), hours, threshold)
        if r is None:
            continue
        side_ = side(r, direction)
        half = "a" if ts < split else "b"
        entry = es.Entry(idx, bars[idx].open, ts)
        for name, pol in tpls.items():
            res = es.replay(bars, entry, direction, pol)
            if res is None:
                continue
            cells[(src, r.regime, side_, name)][half].append(res.r_multiple)
            cells[("ALL", r.regime, side_, name)][half].append(res.r_multiple)
    return cells


def _mean(xs):
    return sum(xs) / len(xs) if xs else 0.0


def best_cells(cells, min_n=MIN_N):
    """Per (source, regime, side): the template with the best combined mean R
    among those positive in BOTH halves with n >= min_n in each, else None."""
    groups = defaultdict(list)
    for (src, reg, side, name), v in cells.items():
        a, b = v["a"], v["b"]
        groups[(src, reg, side)].append((name, a, b))
    out = {}
    for key, rows in groups.items():
        ok = [(name, _mean(a + b), len(a), len(b), _mean(a), _mean(b))
              for name, a, b in rows
              if len(a) >= min_n and len(b) >= min_n and _mean(a) > 0 and _mean(b) > 0]
        out[key] = max(ok, key=lambda x: x[1]) if ok else None
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--bars", required=True, help="JSON list of M1 candle dicts, true UTC")
    ap.add_argument("--since", default="2026-08-20")
    ap.add_argument("--hours", type=int, default=6)
    ap.add_argument("--threshold", type=float, default=0.4)
    ap.add_argument("--json")
    a = ap.parse_args(argv)
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp()
    bars = es.bars_from(json.load(open(a.bars)))
    h1 = h1_from_m1(bars)
    sigs = signals(a.db, since)
    tpls = templates(a.db)
    cells = study(sigs, bars, h1, tpls, a.hours, a.threshold)
    print(f"{len(sigs)} signals, {len(tpls)} templates, H1 {a.hours}h ER>={a.threshold}")
    best = best_cells(cells)
    for key in sorted(best):
        b = best[key]
        n = sum(len(cells[(key[0], key[1], key[2], t)]["a"]) + len(cells[(key[0], key[1], key[2], t)]["b"])
                for t in tpls if (key[0], key[1], key[2], t) in cells) // max(len(tpls), 1)
        print(f"{key[0][:28]:28} {key[1]:5} {key[2]:7} n~{n:4}  "
              + (f"{b[0][:28]:28} R {b[1]:+.3f} (halves {b[4]:+.3f} / {b[5]:+.3f})" if b else "STAND_DOWN"))
    if a.json:
        json.dump({"|".join(k): {"a": v["a"], "b": v["b"]} for k, v in cells.items()},
                  open(a.json, "w"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
