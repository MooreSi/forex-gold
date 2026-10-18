"""Bounded, offline entry trials. No broker, databases, training or promotion.

Inputs are immutable decision opportunities and UTC bid/ask observations with
their local availability times. Context must have been available at decision.
The same simulator, fixed barriers and exposure rule serve every comparison.
"""
from __future__ import annotations
from dataclasses import asdict, dataclass
from collections import defaultdict
from statistics import NormalDist
import hashlib
import json
import math

import numpy as np

HOLDOUT_START = 1792368000.0  # 2026-10-19 00:00 UTC, after this protocol was registered
FAMILIES = ("structural_pullback", "breakout_retest", "failed_continuation")


@dataclass(frozen=True)
class Protocol:
    version: str = "entry-trials-v1"
    rr: float = 2.0
    retracement_atr: float = .25
    pending_s: int = 300
    hold_s: int = 3600
    min_fills: int = 200
    min_days: int = 30
    cost_stress_pts: float = .25
    trials: int = 3
    holdout_start: float = HOLDOUT_START


def identity(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


def qualifies(family, context):
    """Three fixed hypotheses, never a parameter grid or an inferred fact."""
    try:
        if family == "structural_pullback":
            return (context["fresh_break"] is True and context["touch_count"] == 0
                    and context["trend_aligned"] is True and float(context["room_r"]) >= 2)
        if family == "breakout_retest":
            return (context["fresh_break"] is True and context["touch_count"] == 0
                    and context["acceptance_closes"] >= 2
                    and 0 < float(context["compression_atr"]) <= 2.5)
        if family == "failed_continuation":
            return (context["failed_continuation"] is True and context["spread_recovered"] is True
                    and context["flow_against_break"] is True and context["flow_source"] == "exchange"
                    and 0 <= float(context["flow_age_s"]) <= 1)
    except (KeyError, TypeError, ValueError):
        return False
    return False


def _quotes(quotes):
    out = []
    for q in quotes:
        values = [float(q[k]) for k in ("ts", "available_at", "bid", "ask")]
        ts, seen, bid, ask = values
        if not all(math.isfinite(v) for v in values) or bid <= 0 or ask < bid or ts > seen + 1:
            raise ValueError("finite executable UTC quotes and availability times required")
        out.append({**q, **dict(zip(("ts", "available_at", "bid", "ask"), values))})
    return sorted(out, key=lambda q: q["available_at"])


def simulate(opportunity, quotes, wait=False, protocol=Protocol()):
    t0 = float(opportunity["decision_ts"])
    if not math.isfinite(t0):
        raise ValueError("finite decision time required")
    try:
        cost = float(opportunity["cost_pts"])
        if not math.isfinite(cost) or cost < 0:
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise ValueError("explicit finite incremental cost required") from None
    side = opportunity["direction"]
    if side not in ("BUY", "SELL"):
        raise ValueError("BUY or SELL direction required")
    sign = 1 if side == "BUY" else -1
    stop, atr = float(opportunity["stop"]), float(opportunity["atr"])
    if not math.isfinite(stop) or not math.isfinite(atr) or atr <= 0:
        raise ValueError("finite structural stop and positive ATR required")
    path = [q for q in _quotes(quotes) if q["available_at"] >= t0]
    base = {"filled": False, "r": 0.0, "censored": False, "exit_ts": t0,
            "reason": "no fill", "risk": None}
    if not path or path[0]["available_at"] - t0 > 60:
        return {**base, "r": None, "censored": True, "reason": "entry quote unavailable"}
    reference = float(path[0]["ask"] if sign == 1 else path[0]["bid"])
    risk = sign * (reference - stop)
    if risk <= 0:
        return {**base, "reason": "invalidated before fill"}
    target = reference + sign * protocol.rr * risk
    limit = reference - sign * protocol.retracement_atr * atr
    deadline, expiry = t0 + protocol.hold_s, t0 + protocol.pending_s
    entry, entered = None, None
    for q in path:
        seen = float(q["available_at"])
        price = float(q["bid"] if sign == 1 else q["ask"])
        paid = float(q["ask"] if sign == 1 else q["bid"])
        if entry is None:
            if wait and seen > expiry:
                return {**base, "exit_ts": expiry, "risk": risk, "reason": "expired unfilled"}
            if sign * (price - stop) <= 0:
                return {**base, "exit_ts": seen, "risk": risk, "reason": "invalidated before fill"}
            if wait and sign * (paid - limit) > 0:
                continue
            entry, entered = paid, seen
        reason, exit_price = None, None
        if seen >= deadline:
            if seen - deadline > 60:
                break
            reason, exit_price = "time", price
        elif sign * (price - stop) <= 0:
            reason, exit_price = "stop", price  # gaps cannot fill at an invented better stop
        elif sign * (price - target) >= 0:
            reason, exit_price = "target", target
        if reason:
            move = sign * (exit_price - entry) - cost
            return {**base, "filled": True, "entry": entry, "entry_ts": entered,
                    "exit_ts": seen, "exit_price": exit_price, "reason": reason,
                    "risk": risk, "r": move / risk,
                    "trade_r": move / (sign * (entry - stop))}
    if entry is None and path[-1]["available_at"] >= expiry:
        return {**base, "exit_ts": expiry, "risk": risk, "reason": "expired unfilled"}
    return {**base, "filled": entry is not None, "entry": entry, "risk": risk,
            "exit_ts": deadline, "r": None, "censored": True, "reason": "incomplete quote path"}


def _lower(rows, key, protocol):
    days = defaultdict(list)
    for row in rows:
        days[int(row["decision_ts"] // 86400)].append(float(row[key]))
    if len(days) < 2:
        return None
    # Day-cluster sandwich SE for the per-opportunity mean; unequal day sizes
    # are retained rather than treating every day or overlapping trade as IID.
    values = [x for group in days.values() for x in group]
    mean = float(np.mean(values))
    g = len(days)
    se = math.sqrt(g / (g - 1) * sum(sum(x - mean for x in group) ** 2 for group in days.values())) / len(values)
    z = NormalDist().inv_cdf(1 - .05 / protocol.trials)
    return mean - z * se


def judge(rows, protocol=Protocol()):
    rows = sorted(rows, key=lambda r: r["decision_ts"])
    settled = [r for r in rows if r.get("r") is not None and r.get("baseline_r") is not None]
    fills = [r for r in settled if r["filled"]]
    data = [{**r, "delta": r["r"] - r["baseline_r"],
             "stressed": r["r"] - (protocol.cost_stress_pts / r["risk"] if r["filled"] else 0)}
            for r in settled]
    lower, delta, stressed = (_lower(data, k, protocol) for k in ("r", "delta", "stressed"))
    days = len({int(r["decision_ts"] // 86400) for r in fills})
    half = len(data) // 2
    positive_halves = bool(half and data[half:] and sum(r["r"] for r in data[:half]) > 0
                           and sum(r["r"] for r in data[half:]) > 0)
    gains = sum(max(0, r["r"]) for r in fills)
    losses = -sum(min(0, r["r"]) for r in fills)
    eq = np.r_[0.0, np.cumsum([r["r"] for r in data])]
    gates = {"enough_fills": len(fills) >= protocol.min_fills,
             "enough_days": days >= protocol.min_days,
             "complete_paths": len(settled) == len(rows) and not any(r.get("censored") for r in rows),
             "forward_only": bool(rows) and all(r.get("forward") and r["decision_ts"] >= protocol.holdout_start for r in rows),
             "positive_halves": positive_halves,
             "positive_lower": lower is not None and lower > 0,
             "incremental_lower": delta is not None and delta > 0,
             "cost_stress_lower": stressed is not None and stressed > 0}
    return {"opportunities": len(rows), "fills": len(fills), "days": days,
            "censored": len(rows) - len(settled), "total_r": sum(r["r"] for r in data),
            "expectancy_per_opportunity_r": float(np.mean([r["r"] for r in data])) if data else None,
            "expectancy_per_fill_r": float(np.mean([r["r"] for r in fills])) if fills else None,
            "fill_rate": len(fills) / len(rows) if rows else 0,
            "profit_factor_r": gains / losses if losses else None,
            "max_drawdown_r": float(max(np.maximum.accumulate(eq) - eq)),
            "lower_r": lower, "paired_delta_lower_r": delta, "stressed_lower_r": stressed,
            "gates": gates, "review_eligible": all(gates.values()), "deployable": False}


def run(opportunities, quotes, protocol=Protocol()):
    quotes = _quotes(quotes)
    ordered = sorted(opportunities, key=lambda o: o["decision_ts"])
    seen, grouped = set(), defaultdict(list)
    busy = defaultdict(lambda: float("-inf"))
    for op in ordered:
        if not all(math.isfinite(float(op[k])) for k in ("decision_ts", "context_available_at")):
            raise ValueError("finite decision and context availability times required")
        if op["id"] in seen:
            raise ValueError("opportunity identifiers must be unique")
        seen.add(op["id"])
        if op["context_available_at"] > op["decision_ts"]:
            raise ValueError("context was not available at decision time")
        family = op["family"]
        if family not in FAMILIES:
            raise ValueError("unknown registered trial")
        results = {}
        for variant in ("baseline", "candidate"):
            key = (family, variant)
            selected = variant == "baseline" or qualifies(family, op["context"])
            if selected and op["decision_ts"] >= busy[key]:
                result = simulate(op, quotes, wait=variant == "candidate", protocol=protocol)
                busy[key] = result["exit_ts"]
            else:
                result = {"filled": False, "r": 0.0, "risk": None, "censored": False,
                          "reason": "filtered" if not selected else "exposure occupied"}
            results[variant] = result
        candidate, baseline = results["candidate"], results["baseline"]
        grouped[family].append({**candidate, "id": op["id"], "decision_ts": op["decision_ts"],
                                "forward": op["decision_ts"] >= protocol.holdout_start,
                                "censored": candidate["censored"] or baseline["censored"],
                                "baseline_r": baseline["r"], "baseline": baseline})
    return {"protocol": asdict(protocol), "protocol_hash": identity(asdict(protocol)),
            "dataset_hash": identity({"opportunities": ordered, "quotes": quotes}),
            "mode": "research only; no automatic promotion",
            "assumptions": ["provided quotes only; unobserved intragap crossings remain unknown",
                "R uses the same offered risk and absolute stop/target for paired entries",
                "resting fills assume executable quotes cross the limit; queue position is unmodelled",
                "one open or pending position per family and variant"],
            "trials": {name: {"summary": judge(grouped[name], protocol), "rows": grouped[name]} for name in FAMILIES}}
