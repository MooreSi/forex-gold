#!/usr/bin/env python3
"""Expectancy by market state: trend with / against / range, volatility,
session, and regime by volatility. docs/todo/017 item 4.

The arithmetic lives in `backend/src/services/analytics/regime_table.py`
(tested there); this file reads and prints.

READ-ONLY. Opens `reversal_engine.db` / `breakout_signal.db` through `mode=ro`
URIs and touches no broker, network or model. The state comes from
`market_state_json`, which the research loop's minute sweep fills one day per
pass, newest first; rows it has not reached are counted as unmeasured.

Label:
  reversal  the virtual realised R (`_realised_r`); executed rows are left
            out, as the model leaves them out with its switch off
  breakout  realised R (`ml_labels.realised_r`)

Usage:
    .venv/bin/python -m tools.regime_table [--engine reversal|breakout]
                                           [--since 2026-07-20] [--db PATH]
"""
from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from backend.src.config import DATA_DIR
from backend.src.services.analytics import regime_table as rt
from backend.src.services.breakout_signal import ml_labels
from backend.src.services.reversal_engine.ml_engine._training_data import _realised_r

_SQL = {
    "reversal": ("reversal_engine.db",
                 "SELECT created_at, trigger_time, live_exec_status, sl_dist, net_pnl_dollars,"
                 " market_state_json FROM re_signals WHERE status='closed'"
                 " AND outcome IN ('win','loss','be') AND created_at >= ?"),
    "breakout": ("breakout_signal.db",
                 "SELECT created_at, trigger_time, live_exec_status, sl_dist, lot_size,"
                 " net_pnl_dollars, pnl_dollars, market_state_json FROM bo_signals"
                 " WHERE status='closed' AND outcome IN ('win','loss','be') AND created_at >= ?"),
}


def rows_for(engine: str, since: float, db: Path | None = None) -> list[dict]:
    name, sql = _SQL[engine]
    path = db or (DATA_DIR / name)
    c = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        raw = [dict(r) for r in c.execute(sql, (since,))]
    finally:
        c.close()
    label = _realised_r if engine == "reversal" else ml_labels.realised_r
    out = []
    for r in raw:
        try:
            state = json.loads(r["market_state_json"]) if r.get("market_state_json") else None
        except (TypeError, ValueError):
            state = None
        out.append({"ts": r["trigger_time"] or r["created_at"], "r": label(r), "state": state})
    return out


def _fmt(cell: str, s: dict) -> str:
    if not s["n"]:
        return f"  {cell:<26} n=0"
    ci = (f"[{s['mean_r'] - rt.Z95 * s['se']:+.3f}, {s['mean_r'] + rt.Z95 * s['se']:+.3f}]"
          if s["se"] is not None else "[    n/a     ]")
    halves = f"{s['first_half'] if s['first_half'] is not None else float('nan'):+.3f} / " \
             f"{s['second_half'] if s['second_half'] is not None else float('nan'):+.3f}"
    return (f"  {cell:<26} n={s['n']:>5}  mean={s['mean_r']:+.3f}R  95%={ci}  "
            f"halves={halves}  {'HOLDS' if s['holds'] else ''}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--engine", choices=sorted(_SQL), default="reversal")
    ap.add_argument("--since", default="2026-07-20")
    ap.add_argument("--db", type=Path, default=None)
    a = ap.parse_args()
    since = datetime.fromisoformat(a.since).replace(tzinfo=timezone.utc).timestamp()
    t = rt.table(rows_for(a.engine, since, a.db))
    print(f"{a.engine}: {t['all']['n']} closed with a state, "
          f"{t['unmeasured']} not yet measured, {t['unlabelled']} without a label")
    print(_fmt("all", t["all"]))
    for axis in ("regime", "vol_state", "session", "regime x vol"):
        print(f"\n{axis}")
        for cell, s in t[axis].items():
            print(_fmt(cell, s))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
