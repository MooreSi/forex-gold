"""Does the Reversal Engine beat chance? docs/todo/reversal-engine/220.

A driftless random walk started between a stop `SL` points away and a target
`TP` points away reaches the target first with probability `SL / (SL + TP)`.
That is the gambler's-ruin result, and it is the no-edge assumption
Black-Scholes is built on. So it is the win rate an entry with NO
directional skill would score with the same geometry, and the only honest
thing to compare the engine's win rate to.

Measured 2026-09-22 over 1,902 virtual signals since 1 Sep: chance 75.4%,
engine 75.2%. A 75% win rate that is all geometry, and a loss after costs.

**Read-only. Nothing here feeds a trading decision.** The panel is the only
reader.

Two things this does NOT model, stated so the numbers are read correctly:
"win" is the engine's own outcome (its first target before its stop), not the
template's partial-close ladder; and costs are not in the chance rate, which
is why `mean_net` sits beside it -- beating chance on wins is necessary, not
sufficient.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Iterable, Optional, Sequence

# Verdict bar. Three, not two: ~40 groups are scored on every load, and at
# z >= 2 one of them clears by luck nearly every time.
Z_BAR = 3.0
MIN_N = 100
RECENT_DAYS = 14


def chance_of_target(row: dict) -> Optional[float]:
    """P(target before stop) under no edge, or None when the row lacks either
    distance. None, never 1.0: a target at the entry is not a certain win the
    engine failed to beat, it is a row that cannot be scored."""
    try:
        sl = float(row.get("sl_dist") or 0.0)
        tp1 = row.get("tp1")
        entry = row.get("trigger_price") or row.get("price_at_signal")
        if sl <= 0 or tp1 is None or entry is None:
            return None
        tp = abs(float(tp1) - float(entry))
        if tp <= 0:
            return None
        return sl / (sl + tp)
    except (TypeError, ValueError):
        return None


def _verdict(n: int, z: Optional[float]) -> str:
    if n < MIN_N:
        return "too few"
    if z is not None and z >= Z_BAR:
        return "beats chance"
    if z is not None and z <= -Z_BAR:
        return "worse than chance"
    return "no evidence"


def score(rows: Sequence[dict]) -> dict:
    """One group's comparison. `rows` must already be scorable win/loss rows."""
    ps = [chance_of_target(r) for r in rows]
    pairs = [(p, r) for p, r in zip(ps, rows) if p is not None]
    n = len(pairs)
    if n == 0:
        return {"n": 0, "expected_win_pct": None, "actual_win_pct": None,
                "excess_pct": None, "z": None, "mean_net": None,
                "verdict": "too few"}
    wins = sum(1 for _, r in pairs if r.get("outcome") == "win")
    expected = sum(p for p, _ in pairs)
    var = sum(p * (1 - p) for p, _ in pairs)
    z = (wins - expected) / math.sqrt(var) if var > 0 else None
    nets = [float(r["net_pnl_dollars"]) for _, r in pairs
            if r.get("net_pnl_dollars") is not None]
    return {
        "n": n,
        "expected_win_pct": round(100.0 * expected / n, 2),
        "actual_win_pct": round(100.0 * wins / n, 2),
        "excess_pct": round(100.0 * (wins - expected) / n, 2),
        "z": None if z is None else round(z, 2),
        "mean_net": round(sum(nets) / len(nets), 2) if nets else None,
        "verdict": _verdict(n, z),
    }


def _bias_alignment(row: dict) -> str:
    bias = str(row.get("htf_bias_at_fill") or row.get("htf_bias") or "").lower()
    direction = str(row.get("direction") or "").upper()
    if bias not in ("bullish", "bearish") or direction not in ("BUY", "SELL"):
        return "neutral"
    return "with" if (bias == "bullish") == (direction == "BUY") else "against"


def _when(row: dict) -> float:
    return float(row.get("trigger_time") or row.get("created_at") or 0.0)


_GROUPINGS = {
    "level_type": lambda r: str(r.get("level_type") or "unknown"),
    "session": lambda r: str(r.get("session") or "unknown"),
    "bias": _bias_alignment,
    "hour": lambda r: str(datetime.fromtimestamp(_when(r), timezone.utc).hour),
}


def _executed(row: dict) -> bool:
    return row.get("live_exec_status") == "executed"


def _auc(scored: list[tuple[float, int]]) -> Optional[float]:
    from backend.src.services.reversal_engine.meta_label import auc
    return auc([x for x, _ in scored], [y for _, y in scored])


def _ml_auc(rows: Iterable[dict]) -> dict:
    """How well the ML gate's creation-time score ranked "made money".

    Out of sample by construction: `ml_prob` was written when the signal was
    created, before its outcome existed.
    """
    scored = [(float(r["ml_prob"]), 1 if float(r["net_pnl_dollars"]) > 0 else 0)
              for r in rows
              if r.get("ml_prob") is not None and r.get("net_pnl_dollars") is not None]
    auc = _auc(scored)
    return {"n": len(scored), "auc": None if auc is None else round(auc, 3)}


def _window(rows: list[dict], days: Optional[int]) -> dict:
    """The verdicts are over VIRTUAL rows only. An executed trade is closed by
    the EA template, whose target and stop are not this row's `tp1` and
    `sl_dist`, so its chance rate here is the wrong one: on 2026-09-23 that
    read 874 executed trades as z = -11 and dragged the overall verdict with
    them. They are reported apart, flagged `geometry: "engine"`, and kept out
    of every group. The ML record is over all rows -- whether a score ranked
    "made money" does not depend on the exit geometry."""
    virtual = [r for r in rows if not _executed(r)]
    groups = {}
    for key, fn in _GROUPINGS.items():
        buckets: dict[str, list[dict]] = {}
        for r in virtual:
            buckets.setdefault(fn(r), []).append(r)
        groups[key] = sorted(({"name": name, **score(rs)} for name, rs in buckets.items()),
                             key=lambda g: -g["n"])
    executed = {**score([r for r in rows if _executed(r)]), "geometry": "engine"}
    return {"days": days, "overall": score(virtual), "groups": groups,
            "executed": executed, "ml_auc": _ml_auc(rows)}


def benchmark(rows: Iterable[dict], now: float) -> dict:
    """The full report: every closed win/loss row, then the last 14 days."""
    usable = [r for r in rows or ()
              if r.get("outcome") in ("win", "loss") and chance_of_target(r) is not None]
    cutoff = now - RECENT_DAYS * 86400
    recent = [r for r in usable if _when(r) >= cutoff]
    return {"all": _window(usable, None), "recent": _window(recent, RECENT_DAYS),
            "z_bar": Z_BAR, "min_n": MIN_N}
