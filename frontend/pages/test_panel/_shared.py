"""Formatting helpers shared by the Bounce panel's sections.

Here rather than in __init__.py so _sections.py can use them without importing
back out of the package, which __init__ already imports from. Same shape as
frontend/pages/breakout_panel/_shared.py.
"""
from datetime import datetime
from typing import Optional

from backend.src.controllers import engines_controller

# Local/Remote switching now lives in the bounce panel_data service:
# in Remote mode these read the VPS's mirrored stats instead of this
# node's own, and the page cannot tell the difference.
_ml_thresh = engines_controller.bounce.ml_thresholds()


# ── Formatting helpers ────────────────────────────────────────────────────────

def _fmt_ts(ts: Optional[float]) -> str:
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts)).strftime("%d %b %H:%M")
    except Exception:
        return "—"


def _fmt_duration(seconds: float) -> str:
    """Format a duration in seconds as a human-readable string: 2h 15m / 45m / 30s."""
    if seconds <= 0:
        return "—"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    h = m // 60
    rem = m % 60
    return f"{h}h {rem}m" if rem else f"{h}h"


def _dir_color(direction: str) -> str:
    return "text-green-400" if direction.upper() == "BUY" else "text-red-400"


def _outcome_color(outcome: str) -> str:
    return {
        "win":      "text-green-400",
        "loss":     "text-red-400",
        "be":       "text-yellow-400",
        "tp1_hit":  "text-blue-300",
        "open":     "text-gray-400",
        "pending":  "text-gray-400",
        "expired":  "text-gray-600",
    }.get((outcome or "").lower(), "text-gray-400")


def _status_badge_color(status: str) -> str:
    return {
        "pending":   "bg-yellow-700 text-yellow-100",
        "triggered": "bg-blue-700 text-blue-100",
        "closed":    "bg-gray-700 text-gray-300",
        "expired":   "bg-gray-800 text-gray-500",
    }.get((status or "").lower(), "bg-gray-700 text-gray-300")


def _pnl_color(val: Optional[float]) -> str:
    if val is None:
        return "text-gray-400"
    return "text-green-400" if float(val) >= 0 else "text-red-400"


def _pnl_str(val: Optional[float], prefix: str = "") -> str:
    if val is None:
        return "—"
    return f"{prefix}{float(val):+.2f}"
