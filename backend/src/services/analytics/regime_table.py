"""Expectancy by market-state cell. docs/todo/017 item 4.

Rows are `{"ts", "r", "state"}`: when the trade was live, its R, and the
`market/market_state` snapshot. Per cell: count, mean R, a standard error
clustered by `CLUSTER_S` blocks (signals minutes apart ride one move, and the
textbook formula overstates the evidence, engines README 2026-09-24), and the
mean in each half of the sample split by time. A cell "holds" only when both
halves agree in sign with the whole and the 95% interval excludes zero: the
bar 014 used, under which no regime cell on 985 signals held.

Pure. The database reads are in `tools/regime_table.py`.
"""
from __future__ import annotations

import math
from typing import Optional

from backend.src.services.market.market_state import regime_cell

CLUSTER_S = 2 * 3600.0
Z95 = 1.96


def cell_stats(rows: list, split_ts: Optional[float] = None) -> dict:
    n = len(rows)
    if n == 0:
        return {"n": 0, "mean_r": None, "se": None, "clusters": 0,
                "first_half": None, "second_half": None, "holds": False}
    mean = sum(x["r"] for x in rows) / n
    clusters: dict[int, list[float]] = {}
    for x in rows:
        clusters.setdefault(int(float(x["ts"]) // CLUSTER_S), []).append(x["r"])
    g = len(clusters)
    se = None
    if g >= 2:
        ss = sum((sum(v) - len(v) * mean) ** 2 for v in clusters.values())
        se = math.sqrt(ss * g / (g - 1)) / n
    first = [x["r"] for x in rows if split_ts is not None and float(x["ts"]) < split_ts]
    second = [x["r"] for x in rows if split_ts is not None and float(x["ts"]) >= split_ts]
    f = sum(first) / len(first) if first else None
    s = sum(second) / len(second) if second else None
    holds = (se is not None and f is not None and s is not None and mean != 0
             and abs(mean) - Z95 * se > 0
             and (f > 0) == (mean > 0) and (s > 0) == (mean > 0))
    return {"n": n, "mean_r": round(mean, 4),
            "se": round(se, 4) if se is not None else None, "clusters": g,
            "first_half": round(f, 4) if f is not None else None,
            "second_half": round(s, 4) if s is not None else None,
            "holds": bool(holds)}


def _keyed(rows: list, key) -> dict:
    out: dict[str, list] = {}
    for x in rows:
        k = key(x["state"])
        if k is not None:
            out.setdefault(k, []).append(x)
    return out


def table(rows: list) -> dict:
    """{axis: {cell: stats}} for regime, volatility, session and the regime
    by volatility cross, plus how many rows had no state or no label."""
    unlabelled = sum(1 for x in rows if x.get("r") is None)
    labelled = [x for x in rows if x.get("r") is not None]
    unmeasured = sum(1 for x in labelled if not x.get("state"))
    usable = [x for x in labelled if x.get("state")]
    ts = sorted(float(x["ts"]) for x in usable)
    split = ts[len(ts) // 2] if ts else None

    def _cross(st):
        c, v = regime_cell(st), st.get("vol_state")
        return f"{c} | {v}" if c and v else None

    axes = {
        "regime": regime_cell,
        "vol_state": lambda st: st.get("vol_state"),
        "session": lambda st: st.get("session"),
        "regime x vol": _cross,
    }
    out: dict = {name: {k: cell_stats(v, split) for k, v in sorted(_keyed(usable, f).items())}
                 for name, f in axes.items()}
    out["all"] = cell_stats(usable, split)
    out["unmeasured"] = unmeasured
    out["unlabelled"] = unlabelled
    return out
