"""Human-readable labels for a trade's close reason, source channel and strategy.

Moved verbatim from `controllers/history/controller.py`; the existing tests in
`tests/controllers/test_history_controller.py` cover these bodies unchanged.

Service-local by the utils rule -- only analytics consumes these. Promote to
`src/utils/` when a second service needs them, not in anticipation.
"""
from __future__ import annotations

from backend.src.utils.models import STRATEGY_NAMES

__all__ = [
    "STRATEGY_LABELS", "parse_reason", "strategy_display_label",
    "trade_source_label", "trade_channel_label",
]

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


def strategy_display_label(strategy: str) -> str:
    """Human-readable label for a trade's strategy, including EA Templates.

    EA Templates arrive as "template:<name>" and are user-defined, so they were
    never in STRATEGY_NAMES/STRATEGY_LABELS and fell through to the "—"
    placeholder. Confirmed live 2026-07-23 that every EA Template trade showed a
    blank Strategy column in Trade Analysis.
    """
    if not strategy:
        return "—"
    from backend.src.services.broker import ea_templates as _et
    if _et.is_template_override(strategy):
        return f"Template: {_et.template_name_from_override(strategy)}"
    return STRATEGY_LABELS.get(strategy, STRATEGY_NAMES.get(strategy, "—"))


def trade_source_label(tg_source: str) -> str:
    """Return a short human-readable label for where a trade originated.

    tg_source values:
      - "manual_market"  → placed via Market Order button
      - "MT5_imported"   → position imported from MT5 sync
      - channel name     → Telegram signal (auto-executed, activated, or IME)
      - "" / None        → manually created signal via New Signal form
    """
    if not tg_source:
        return "Manual Signal"
    if tg_source == "manual_market":
        return "Manual Market"
    if tg_source == "MT5_imported":
        return "MT5 Import"
    # Strip legacy "instant:" prefix stored in older DB records
    if tg_source.startswith("instant:"):
        tg_source = tg_source[len("instant:"):]
    return tg_source


# Sources that are this app's own, not a Telegram channel. The engines write
# their name into `tg_source` because that column is "where the trade came
# from", and a reader that assumes every value is a channel calls the app's
# own biggest source of trades a channel -- which is how an engine execution
# became indistinguishable from a copied signal (2026-09-21).
#
# Exact names. A substring test would silence a real channel that happens to
# contain one of these words.
INTERNAL_ENGINE_SOURCES = frozenset({
    "Reversal Engine", "Breakout Engine", "Signal Generator", "Bounce Generator",
})
# The ORB/IVB report names its own run, so it is matched by prefix.
_INTERNAL_PREFIXES = ("ORB/IVB Report",)
# Neither an engine nor a channel: the operator, or a sync from the broker.
_NOT_A_SOURCE = ("manual_market", "MT5_imported")


def is_internal_engine(tg_source: str) -> bool:
    """Whether this trade came from one of the app's own engines."""
    if not tg_source:
        return False
    return (tg_source in INTERNAL_ENGINE_SOURCES
            or tg_source.startswith(_INTERNAL_PREFIXES))


def trade_channel_label(tg_source: str) -> str:
    """Return the Telegram channel name, or empty string if not a Telegram signal."""
    if not tg_source or tg_source in _NOT_A_SOURCE or is_internal_engine(tg_source):
        return ""
    if tg_source.startswith("instant:"):
        return tg_source[len("instant:"):]
    return tg_source
