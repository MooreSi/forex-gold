"""One place that reads the new capability switches.

Every capability from `docs/todo/reversal-engine/200` ships behind its own
column in `vantage_risk_settings` (migration 41) and every one of them is
OFF. This module turns those columns into the config objects the pure
modules take, so there is a single answer to "is this on" rather than one
per call site -- which is how a gate ends up enabled in one path and not
another, and how nobody can say afterwards what was running.

Two properties are load-bearing and both are pinned by tests:

  * **A default settings row leaves everything inert.** Not "mostly
    inert": `sizing_inputs` out of a default row returns a `SizingInputs`
    that multiplies a lot size by exactly 1.0.
  * **A settings row MISSING these columns behaves the same.** A client
    that has not run migration 41 must trade exactly as it did, rather
    than crashing or silently enabling something.
"""
from __future__ import annotations

from typing import Optional

from backend.src.services.market import entry_trigger as _trigger
from backend.src.services.risk import event_tiers as _events
from backend.src.services.risk import session_liquidity as _liquidity
from backend.src.services.risk import sizing_policy as _sizing


def _on(rs: dict, key: str) -> bool:
    return bool(rs.get(key, 0))


def _num(rs: dict, key: str, default: float) -> float:
    try:
        v = rs.get(key)
        return default if v is None else float(v)
    except (TypeError, ValueError):
        return default


def atr_barrier_config(rs: dict) -> Optional[dict]:
    """Config for `signal_generator.atr_barriers`, or None when off.

    None rather than `{"enabled": False}` so a caller cannot accidentally
    pass a disabled config into something that only checks for presence.
    """
    if not _on(rs, "re_atr_barriers_enabled"):
        return None
    return {"enabled": True,
            "stop_mult": _num(rs, "re_atr_stop_mult", 1.2),
            "tp1_mult": _num(rs, "re_atr_tp1_mult", 1.2)}


def entry_trigger_config(rs: dict) -> Optional[_trigger.TriggerConfig]:
    """Config for `entry_trigger.confirm`, or None when off.

    An enabled gate with no checks selected is returned as a real config
    with nothing required, which confirms everything. That is what the
    switches say, and it is deliberately not reinterpreted as either "block
    everything" or "turn some checks on for them".
    """
    if not _on(rs, "entry_trigger_enabled"):
        return None
    return _trigger.TriggerConfig(
        require_rejection=_on(rs, "entry_trigger_rejection"),
        require_deceleration=_on(rs, "entry_trigger_deceleration"),
        max_range_ratio=_num(rs, "entry_trigger_max_range_ratio", 0.5),
    )


def meta_label_gate(rs: dict) -> tuple[bool, float]:
    """`(enabled, threshold)` for the meta-labeller."""
    return (_on(rs, "meta_label_gate_enabled"),
            _num(rs, "meta_label_threshold", 0.5))


def liquidity_map_enabled(rs: dict) -> bool:
    return _on(rs, "liquidity_map_levels_enabled")


def liquidity_blocks(now_ts: float, rs: dict,
                     events: Optional[list] = None) -> Optional[str]:
    """A reason to stand aside on liquidity grounds, or None.

    Two independent switches behind one question, because the call site's
    question is one question. The clock-driven check runs first: "the week
    just opened" is more useful than "an event is near" when both are true.
    """
    if _on(rs, "session_liquidity_gate_enabled"):
        ok, reason = _liquidity.check(now_ts, _liquidity.Config())
        if not ok:
            return reason
    if _on(rs, "event_tier_gate_enabled"):
        ok, reason = _events.check(events or [], _events.Config())
        if not ok:
            return reason
    return None


def sizing_inputs(rs: dict, atr: float, reference_atr: float,
                  drawdown_pct: float,
                  open_correlated_lots: float) -> _sizing.SizingInputs:
    """Inputs for `sizing_policy.apply`.

    The volatility and drawdown scalars share one switch, because they are
    one idea -- size to the risk actually being taken -- and splitting them
    would let a half-applied policy run without anybody choosing that. The
    correlated cap is independent: it is a hard ceiling rather than a
    scalar, and it is useful on its own.
    """
    scale_on = _on(rs, "vol_target_sizing_enabled")
    return _sizing.SizingInputs(
        atr=atr if scale_on else 0.0,
        reference_atr=reference_atr if scale_on else 0.0,
        drawdown_pct=drawdown_pct if scale_on else 0.0,
        open_correlated_lots=open_correlated_lots,
        correlated_cap_lots=_num(rs, "correlated_exposure_cap_lots", 0.0),
    )
