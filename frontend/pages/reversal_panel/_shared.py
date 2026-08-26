"""Small formatting helpers shared by the reversal panel's sections.

They live here rather than in __init__.py so that _sections.py can use them
without importing back out of the package -- which is a circular import, since
__init__ imports _sections.
"""
from datetime import datetime


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
