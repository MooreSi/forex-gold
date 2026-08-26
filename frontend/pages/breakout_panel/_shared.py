"""Formatting helpers shared by the breakout panel's sections.

Here rather than in __init__.py so _sections.py can use them without importing
back out of the package, which __init__ already imports from.
"""
from datetime import datetime
from typing import Optional


from backend.src.controllers import engines_controller

_ml_thresh = engines_controller.breakout.ml_thresholds()


def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d %b %H:%M")
    except Exception:
        return "—"


def _fmt_dur(seconds: float) -> str:
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


def _dir_color(d: str) -> str:
    return "text-green-400" if str(d).upper() == "BUY" else "text-red-400"


def _pnl_color(v) -> str:
    try:
        return "text-green-400" if float(v) >= 0 else "text-red-400"
    except (TypeError, ValueError):
        return "text-gray-400"


def _pnl_str(v, prefix="") -> str:
    try:
        return f"{prefix}{float(v):+.2f}"
    except (TypeError, ValueError):
        return "—"


def _outcome_color(o: str) -> str:
    return {
        "win":  "text-green-400",
        "loss": "text-red-400",
        "be":   "text-yellow-400",
    }.get((o or "").lower(), "text-gray-400")


def _bo_type_badge(btype: str) -> tuple[str, str]:
    """Return (display_text, color) for breakout type."""
    if btype == "go":
        return "BREAK→GO", "bg-orange-700 text-orange-100"
    elif btype == "retest":
        return "RETEST", "bg-blue-700 text-blue-100"
    return btype.upper(), "bg-gray-700 text-gray-300"
