"""Display shaping for the trade-history views.

Step 2 of draining `frontend/pages/history.py`: the pure formatting and label
logic moves out of the page, leaving the page with widget construction and calls
into here. Nothing in this module imports NiceGUI, and nothing touches the
database -- that is the contract that makes `frontend/` replaceable.

Every function is a verbatim move. The bodies are unchanged, so the existing
tests in tests/controllers/test_history_controller.py apply to them without
edits; that they still pass is the proof the move was faithful.

The broker-timestamp handling is the part worth reading before changing
anything. MT5 stores UTC+3 wall-clock as if it were a Unix epoch, so a
timestamp interpreted naively as UTC yields *broker* time -- which is what
`format_broker_ts` deliberately wants for display. `broker_ts_to_uk_date`
subtracts the offset first, because a calendar date has to be a real local date
or the monthly P&L calendar puts trades in the wrong day.
"""
from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from backend.src.utils.models import STRATEGY_NAMES

UK_TZ = ZoneInfo("Europe/London")
BROKER_OFFSET = 10800  # broker stores UTC+3 timestamps as-if-UTC

STRATEGY_LABELS = {
    "scale_out":       "Scale Out",
    "be_runner":       "BE Runner",
    "trail_stop":      "Trail Stop",
    "protected_scale": "Protected Scale",
    "conservative":    "Conservative",
}


def parse_reason(comment: str, pnl: float = 0.0) -> str:
    """Translate a raw MT5 close-deal comment into a human-readable label.

    MT5 close comment patterns (Vantage / standard MT5):
      "[sl 4482.00]"       -> SL
      "[tp 4460.00]"       -> TP
      "so: 1:100"          -> Stop-out (margin call)
      "closePosition"      -> Manual close (via bridge)
      "close"              -> Manual close
      "ForexTrader"        -> App-initiated close
      ""                   -> fallback on PnL sign
    """
    c = comment.strip().lower()
    if c.startswith("[sl") or "stop loss" in c or c == "sl":
        return "SL"
    if c.startswith("[tp") or "take profit" in c or c == "tp":
        return "TP"
    if c.startswith("so:") or "stop out" in c or "margin call" in c:
        return "Stop-out"
    if c in ("closeposition", "close", "forextrader", "manual", ""):
        return "Manual"
    # Partial-close comments from the app
    if "partial" in c or "scale" in c:
        return "Partial TP"
    # Anything left -- return cleaned-up original
    return comment.strip() or ("Win" if pnl > 0 else "Loss")


def format_broker_ts(ts) -> str:
    """Format an MT5 broker timestamp for display.

    Read as UTC on purpose: MT5 encodes UTC+3 as a Unix epoch, so interpreting
    it as UTC yields broker time, which is what the table columns show.
    """
    if not ts:
        return "—"
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).strftime("%m-%d %H:%M")
    except Exception:
        return str(ts)[:16]


def format_duration(seconds: Optional[float]) -> str:
    """Compact human-readable duration -- "45s", "12m", "2h 15m", "3d 4h".

    Used both for how long a closed trade was held (open->close) and for how
    long a Limit Runner / EA Template grid order sat pending before it filled
    (pending_placed_at->open).
    """
    if seconds is None or seconds < 0:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return f"{seconds}s"
    minutes, seconds = divmod(seconds, 60)
    if minutes < 60:
        return f"{minutes}m {seconds}s" if seconds else f"{minutes}m"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        return f"{hours}h {minutes}m" if minutes else f"{hours}h"
    days, hours = divmod(hours, 24)
    return f"{days}d {hours}h" if hours else f"{days}d"


def to_date(ts) -> Optional[date]:
    try:
        return datetime.fromtimestamp(float(ts), tz=timezone.utc).date()
    except Exception:
        return None


def broker_ts_to_uk_date(ts) -> Optional[date]:
    """Convert a broker timestamp (UTC+3-stored-as-UTC) to a UK calendar date.

    Unlike format_broker_ts, the offset must be removed here: this feeds the
    monthly P&L calendar, and a date derived from broker time would file trades
    opened late in the UK evening under the following day.
    """
    try:
        real_utc_epoch = float(ts) - BROKER_OFFSET
        return datetime.fromtimestamp(real_utc_epoch, tz=UK_TZ).date()
    except Exception:
        return None


def strategy_display_label(strategy: str) -> str:
    """Human-readable label for a trade's strategy, including EA Templates.

    EA Templates arrive as "template:<name>" and are user-defined, so they were
    never in STRATEGY_NAMES/STRATEGY_LABELS and fell through to the "—"
    placeholder. Confirmed live 2026-07-23 that every EA Template trade showed a
    blank Strategy column in Trade Analysis.
    """
    if not strategy:
        return "—"
    from forex_trader.core import core_ea_templates as _et
    if _et.is_template_override(strategy):
        return f"Template: {_et.template_name_from_override(strategy)}"
    return STRATEGY_LABELS.get(strategy, STRATEGY_NAMES.get(strategy, "—"))
