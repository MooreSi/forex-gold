"""What fills really cost, for the Dashboard's Fill cost card.

Summarises `execution_quality` (measured per closed trade by broker/tca.py)
over a recent window. Read-only.

Carried over from tca.py: an unmeasured fill is not a free one, so it is
counted in `unmeasured` and never averaged in as zero; and slippage is signed
(positive = against us), so the share of fills that went against us is
reported beside the median. Percentiles are nearest-rank. A figure with
nothing behind it is None, never 0.
"""
from __future__ import annotations

import math
import time
from statistics import median
from typing import Optional

from backend.src.services.broker import tca_repo


def _pct(values: list[float], p: float) -> Optional[float]:
    if not values:
        return None
    s = sorted(values)
    return round(s[max(0, math.ceil(p * len(s)) - 1)], 3)


def _med(values: list[float]) -> Optional[float]:
    return round(median(values), 3) if values else None


def _figures(rows: list[dict]) -> dict:
    cost = [float(r["cost_pts"]) for r in rows if r.get("cost_pts") is not None]
    slip = [float(r["slippage_pts"]) for r in rows if r.get("slippage_pts") is not None]
    spread = [float(r["spread_cost_pts"]) for r in rows if r.get("spread_cost_pts") is not None]
    cost_r = [float(r["cost_r"]) for r in rows if r.get("cost_r") is not None]
    return {
        "n": len(rows),
        "median_cost_pts": _med(cost),
        "p75_cost_pts": _pct(cost, 0.75),
        "p90_cost_pts": _pct(cost, 0.90),
        "median_slippage_pts": _med(slip),
        "adverse_share": round(sum(s > 0 for s in slip) / len(slip), 3) if slip else None,
        "favourable_share": round(sum(s < 0 for s in slip) / len(slip), 3) if slip else None,
        "median_spread_pts": _med(spread),
        "median_cost_r": _med(cost_r),
    }


def summarise(rows: list[dict], days: int = 14, now: Optional[float] = None) -> dict:
    now = now if now is not None else time.time()
    since = now - days * 86400
    recent = [r for r in rows if float(r.get("open_time") or 0) >= since]
    measured = [r for r in recent if r.get("measured") and r.get("cost_pts") is not None]
    groups: dict[str, list[dict]] = {}
    for r in measured:
        groups.setdefault(r.get("strategy") or "unknown", []).append(r)
    by_strategy = sorted(
        ({"strategy": k, **_figures(v)} for k, v in groups.items()),
        key=lambda g: g["n"], reverse=True,
    )[:6]
    return {"days": days, **_figures(measured),
            "unmeasured": len(recent) - len(measured), "by_strategy": by_strategy}


def report(days: int = 14) -> dict:
    return summarise(tca_repo.fetch_costs(), days=days)


async def report_async(days: int = 14) -> dict:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(report, days)
