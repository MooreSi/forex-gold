"""Small formatting helpers shared by the reversal panel's sections.

They live here rather than in __init__.py so that _sections.py can use them
without importing back out of the package -- which is a circular import, since
__init__ imports _sections.
"""
from datetime import datetime
from typing import Optional


from backend.src.controllers import engines_controller

_ml_thresh = engines_controller.reversal.ml_thresholds()


def _fmt_ts(ts) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d %b %H:%M")
    except Exception:
        return "—"


def _fmt_duration(seconds: float) -> str:
    s = int(seconds)
    if s <= 0:
        return "—"
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    return f"{h}h {m % 60}m" if m % 60 else f"{h}h"


def _dir_color(direction: str) -> str:
    return "text-green-400" if str(direction).upper() == "BUY" else "text-red-400"


def _outcome_color(outcome: str) -> str:
    return {
        "win":     "text-green-400",
        "loss":    "text-red-400",
        "be":      "text-yellow-400",
        "open":    "text-gray-400",
        "expired": "text-gray-600",
    }.get((outcome or "").lower(), "text-gray-400")


def _pnl_str(val, prefix: str = "") -> str:
    if val is None:
        return "—"
    return f"{prefix}{float(val):+.2f}"


def _pnl_color(val) -> str:
    if val is None:
        return "text-gray-400"
    return "text-green-400" if float(val) >= 0 else "text-red-400"


def _level_type_badge(ltype: str) -> tuple[str, str]:
    """(badge_text, badge_color)"""
    colors = {
        "asia_low":   ("ASIA LO", "indigo"),
        "asia_high":  ("ASIA HI", "indigo"),
        "swing_high": ("SWING H", "purple"),
        "swing_low":  ("SWING L", "purple"),
        "round_10":   ("ROUND10", "teal"),
        "round_5":    ("ROUND5",  "teal"),
        "congestion": ("CONGST",  "orange"),
    }
    return colors.get(ltype, (ltype[:7].upper(), "grey"))


# Moved here from reversal_panel/__init__.py 2026-09-16 (pure move, no
# logic change): the open-position card moved to _sections.py and needs
# it, and importing it back out of the package __init__ would be circular.
def _live_exec_badge(exec_st: str) -> Optional[tuple[str, str, str]]:
    """(badge_text, badge_color, tooltip) for a non-executed live_exec_status,
    or None if there's nothing worth flagging (empty, or already executed/
    virtual-by-design). Mirrors the reasons written by _try_live_execute /
    _try_re_limit_order in reversal_engine_live_execute.py."""
    if not exec_st or exec_st in ("executed",) or exec_st.startswith("limit_order_placed"):
        return None
    if exec_st == "skipped:live_disabled":
        return None  # live execution off entirely -- not a per-signal problem
    if exec_st == "ml_skipped":
        return ("ML BLOCKED", "orange", "ML gate blocked live execution: predicted R-multiple < 0")
    if exec_st == "bias_skipped":
        return ("BIAS BLOCKED", "orange", "Fill-time bias re-check disagreed with the signal direction")
    if "circuit breaker" in exec_st.lower():
        return ("CIRCUIT BREAKER", "red", exec_st)
    if exec_st.startswith("limit_order_skip"):
        return ("LIMIT ORDER SKIPPED", "red", exec_st.split(":", 1)[-1])
    if exec_st.startswith("limit_order_rejected"):
        return ("EA REJECTED", "red", exec_st.split(":", 1)[-1])
    if exec_st.startswith("limit_order_error"):
        return ("LIMIT ORDER ERROR", "red", exec_st.split(":", 1)[-1])
    if exec_st.startswith("open_failed"):
        return ("OPEN FAILED", "red", exec_st)
    if exec_st.startswith("error"):
        return ("ERROR", "red", exec_st.split(":", 1)[-1] if ":" in exec_st else exec_st)
    return ("NOT EXECUTED", "grey", exec_st)
