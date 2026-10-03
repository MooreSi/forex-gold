"""The bar the Dukascopy replay is judged by (docs/todo/reversal-engine/260).

Fixed in the spec before any result existed, and kept here as code with its
own tests so the report cannot grade itself on a softer curve than the
document. It is `240`'s bar plus a years rule, because the point of years of
history is regimes:

    z >= 3          against the placebo, trades clustered by 2-hour block
    n >= 200        resolved trades
    mean R > 0      after costs, in BOTH halves of history
    mean R > 0      in MORE than two thirds of the calendar years that have
                    at least 200 trades

A slice (a level type, a session, a quintile -- the report prints about
thirty) must reach z >= 4, because thirty looks at z >= 3 produce one by
chance. Pure: rows in, a verdict out.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional, Sequence

from backend.src.services.reversal_engine import entry_study as es

BAR_Z = 3.0
SLICE_Z = 4.0
MIN_N = 200
MIN_YEAR_N = 200
YEAR_SHARE = 2.0 / 3.0


@dataclass
class Judgement:
    passes: bool
    summary: Optional[dict]
    years: dict = field(default_factory=dict)
    counted_years: list = field(default_factory=list)
    failures: list = field(default_factory=list)


def _mean(xs: Sequence[float]) -> float:
    return sum(xs) / len(xs) if xs else float("nan")


def summarise(rows: Sequence[dict], key: str) -> Optional[dict]:
    """Placebo z, win rate and mean R for the rows that resolved."""
    rs = [r for r in rows if r.get(f"{key}_won") is not None
          and r.get(f"{key}_p") is not None]
    if not rs:
        return None
    b = es.beats_placebo([r[f"{key}_won"] for r in rs], [r[f"{key}_p"] for r in rs],
                         [r["cluster"] for r in rs])
    rr = [r for r in rs if r.get(f"{key}_r") is not None]
    cut = sorted(r["ts"] for r in rr)[len(rr) // 2] if rr else 0.0
    b.update(mean_r=_mean([r[f"{key}_r"] for r in rr]),
             r_h1=_mean([r[f"{key}_r"] for r in rr if r["ts"] < cut]),
             r_h2=_mean([r[f"{key}_r"] for r in rr if r["ts"] >= cut]))
    return b


def by_year(rows: Sequence[dict], key: str) -> dict:
    groups: dict = {}
    for r in rows:
        y = datetime.fromtimestamp(r["ts"], timezone.utc).year
        groups.setdefault(y, []).append(r)
    return {y: summarise(g, key) for y, g in sorted(groups.items())}


def judge(rows: Sequence[dict], key: str) -> Judgement:
    s = summarise(rows, key)
    if s is None or s.get("z") is None:
        return Judgement(False, s, failures=["nothing resolved, no z"])
    years = by_year(rows, key)
    counted = [y for y, v in years.items() if v and v["n"] >= MIN_YEAR_N]
    fails = []
    if s["z"] < BAR_Z:
        fails.append(f"z {s['z']:+.2f} < {BAR_Z:g}")
    if s["n"] < MIN_N:
        fails.append(f"n {s['n']} < {MIN_N}")
    if not (s["r_h1"] > 0 and s["r_h2"] > 0):
        fails.append(f"half: R {s['r_h1']:+.3f} / {s['r_h2']:+.3f} is not positive in both")
    pos = [y for y in counted if years[y]["mean_r"] > 0]
    if not counted or len(pos) / len(counted) <= YEAR_SHARE:
        fails.append(f"year: R > 0 in {len(pos)} of {len(counted)} years with "
                     f">= {MIN_YEAR_N} trades, needs more than two thirds")
    return Judgement(not fails, s, years, counted, fails)


def slice_is_signal(summary: Optional[dict]) -> bool:
    """A slice is worth a second look only at z >= 4 with at least 200 rows."""
    if not summary or summary.get("z") is None:
        return False
    return summary["z"] >= SLICE_Z and summary["n"] >= MIN_N
