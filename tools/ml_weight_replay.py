#!/usr/bin/env python3
"""Does any training-weight scheme make the Reversal batch model rank trades
better than today's positional weights? docs/todo/017 step 1b.

The protocol, variants and pass bar are pre-registered in the spec (section
6b) and were fixed before this was first run. The replay logic is
`backend/src/services/reversal_engine/weight_replay.py` (tested there).

READ-ONLY. Opens the database through a `mode=ro` URI (point --db at a copy
to be safe), fits throwaway models in memory, and never loads, saves or
replaces the live model. No broker, no network.

Usage:
    .venv/bin/python -m tools.ml_weight_replay [--db PATH] [--n-test 1500]
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from backend.src.config import DATA_DIR
from backend.src.services.market import sample_weights as sw
from backend.src.services.reversal_engine import weight_replay as wr
from backend.src.services.reversal_engine.ml_engine import _fit_batch
from backend.src.services.reversal_engine.ml_engine._training_data import (
    _realised_r, _span, stored_vector)

VARIANTS = {
    "V0 positional (today)": None,
    "V1 uniqueness only": (None, 1.0),
    "V2 3d, floor 0.25": (3.0, 0.25),
    "V3 14d, floor 0.25": (14.0, 0.25),
    "V4 60d, floor 0.25": (60.0, 0.25),
}
BONFERRONI_LEVEL = 1.0 - 0.05 / 4


def load(db: Path) -> list[dict]:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    try:
        raw = [dict(r) for r in c.execute(
            "SELECT * FROM re_signals WHERE status='closed' AND ml_features_json IS NOT NULL"
            " AND outcome IN ('win','loss','be')")]
    finally:
        c.close()
    rows = []
    for r in raw:
        y = _realised_r(r)
        x = stored_vector(r)
        if y is None or x is None or not r.get("created_at"):
            continue
        start, end = _span(r)
        rows.append({"created_at": float(r["created_at"]), "close_time": end,
                     "span": (start, end), "x": x, "y": y})
    rows.sort(key=lambda r: r["created_at"])
    return rows


def _weights(spec):
    if spec is None:
        return lambda train: None
    hl, floor = spec
    if hl is None:
        return lambda train: sw.training_weights([t["span"] for t in train], 1e9, floor)
    return lambda train: sw.training_weights([t["span"] for t in train], hl, floor)


def _fit(X, y, w):
    model, _, _ = _fit_batch(X, y) if w is None else _fit_batch(X, y, w)
    return model


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--db", type=Path, default=DATA_DIR / "reversal_engine.db")
    ap.add_argument("--n-test", type=int, default=1500)
    ap.add_argument("--block", type=int, default=25)
    a = ap.parse_args()

    rows = load(a.db)
    print(f"{len(rows)} trainable virtual rows; testing the newest {a.n_test}, "
          f"refit every {a.block}")
    results = {}
    for name, spec in VARIANTS.items():
        res = wr.replay(rows, _weights(spec), _fit, a.n_test, a.block)
        results[name] = res
        print(f"  ran {name}: {len(res)} scored")

    base = results["V0 positional (today)"]
    ts = [r["created_at"] for r, _ in base]
    y = [r["y"] for r, _ in base]
    p0 = [p for _, p in base]
    print(f"\nall {len(y)} test signals: mean R {sum(y) / len(y):+.4f}\n")
    print(f"{'variant':<24} {'spearman':>9} {'AUC':>6} {'pass%':>6} {'lift':>8}"
          f"   lift CI            d-spearman CI vs V0   beats V0")
    for name, res in results.items():
        p = [q for _, q in res]
        if [r["created_at"] for r, _ in res] != ts:
            raise RuntimeError(f"{name} scored different rows from V0")
        rho = wr.spearman(p, y)
        auc = _auc(p, y)
        lift = wr.lift(p, y)
        lift_ci = wr.block_bootstrap(
            ts, lambda idx: wr.lift([p[i] for i in idx], [y[i] for i in idx]),
            level=BONFERRONI_LEVEL)
        if name.startswith("V0"):
            d_ci = (None, None)
            beats = "-"
        else:
            def _d(idx):
                a1 = wr.spearman([p[i] for i in idx], [y[i] for i in idx])
                a0 = wr.spearman([p0[i] for i in idx], [y[i] for i in idx])
                return None if a1 is None or a0 is None else a1 - a0
            d_ci = wr.block_bootstrap(ts, _d, level=BONFERRONI_LEVEL)
            beats = "YES" if (d_ci[0] is not None and d_ci[0] > 0
                              and lift_ci[0] is not None and lift_ci[0] > 0) else "no"
        passed = sum(1 for q in p if q >= 0) / len(p)
        print(f"{name:<24} {rho:>+9.4f} {auc:>6.3f} {passed:>6.1%} {lift:>+8.4f}"
              f"   [{_f(lift_ci[0])}, {_f(lift_ci[1])}]   [{_f(d_ci[0])}, {_f(d_ci[1])}]   {beats}")
    return 0


def _f(v):
    return "  n/a  " if v is None else f"{v:+.4f}"


def _auc(p, y):
    from backend.src.services.breakout_signal.ml_labels import auc
    return auc([1 if v > 0 else 0 for v in y], p) or float("nan")


if __name__ == "__main__":
    raise SystemExit(main())
