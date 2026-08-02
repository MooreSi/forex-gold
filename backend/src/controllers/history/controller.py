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
    from backend.src.services.broker import ea_templates as _et
    if _et.is_template_override(strategy):
        return f"Template: {_et.template_name_from_override(strategy)}"
    return STRATEGY_LABELS.get(strategy, STRATEGY_NAMES.get(strategy, "—"))


# ── M3 page drain: the ticket-map builders and DB passthroughs ───────────────
# Moved verbatim from frontend/pages/history.py's nested helpers. Each async
# wrapper runs its builder on the DB worker thread, exactly as the page's own
# `await db_module.to_db_thread(...)` calls did -- these run inside refresh
# handlers on the event loop.

from backend.src.db import database as db_module  # noqa: E402
from backend.src.services.analytics import trade_history_repo  # noqa: E402


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


def trade_channel_label(tg_source: str) -> str:
    """Return the Telegram channel name, or empty string if not a Telegram signal."""
    if not tg_source or tg_source in ("manual_market", "MT5_imported", "Signal Generator", "Bounce Generator"):
        return ""
    if tg_source.startswith("instant:"):
        return tg_source[len("instant:"):]
    return tg_source


def _ticket_source_map_sync(days: int) -> dict[str, str]:
    import time
    cutoff = time.time() - days * 86400
    result: dict[str, str] = {}
    try:
        ledger_channels, _, _ = db_module.get_consolidated_ticket_maps()
        for ticket, tg_source in ledger_channels.items():
            ch = trade_channel_label(tg_source or "")
            result[ticket] = ch if ch else trade_source_label(tg_source or "")
    except Exception:
        pass
    try:
        rows = trade_history_repo.ticket_sources(cutoff)
        for mt5_ticket, tg_source in rows:
            ch = trade_channel_label(tg_source or "")
            result[str(mt5_ticket)] = ch if ch else trade_source_label(tg_source or "")
    except Exception:
        pass
    try:
        # Adaptive Runner ladder legs 2+ are opened as their own raw MT5
        # tickets, tracked only in vantage_ladder_legs -- they never get
        # their own vantage_simulated_trades row; inherit the real channel
        # from the parent trade instead.
        rows = trade_history_repo.ticket_sources_for_legs(cutoff)
        for mt5_ticket, tg_source in rows:
            ch = trade_channel_label(tg_source or "")
            result[str(mt5_ticket)] = ch if ch else trade_source_label(tg_source or "")
    except Exception:
        pass
    return result


def _ticket_strategy_map_sync(days: int) -> dict[str, str]:
    import time
    cutoff = time.time() - days * 86400
    result: dict[str, str] = {}
    try:
        _, ledger_strategies, _ = db_module.get_consolidated_ticket_maps()
        for ticket, strategy in ledger_strategies.items():
            result[ticket] = strategy_display_label(strategy or "")
    except Exception:
        pass
    try:
        rows = trade_history_repo.ticket_strategies(cutoff)
        for mt5_ticket, strategy, dpm_trade_id in rows:
            label = "DPM" if dpm_trade_id else strategy_display_label(strategy or "")
            result[str(mt5_ticket)] = label
    except Exception:
        pass
    try:
        rows = trade_history_repo.ticket_strategies_for_legs(cutoff)
        for mt5_ticket, strategy in rows:
            result[str(mt5_ticket)] = strategy_display_label(strategy or "")
    except Exception:
        pass
    return result


def _ticket_max_tp_map_sync() -> dict[str, str]:
    result: dict[str, str] = {}
    try:
        ledger_max_tp, _ = db_module.get_consolidated_extra_maps()
        result.update(ledger_max_tp)
    except Exception:
        pass
    try:
        result.update(db_module.get_max_tp_map_by_ticket())
    except Exception:
        pass
    return result


def _ticket_rr_map_sync() -> dict[str, float]:
    result: dict[str, float] = {}
    try:
        _, ledger_rr = db_module.get_consolidated_extra_maps()
        result.update(ledger_rr)
    except Exception:
        pass
    try:
        result.update(db_module.get_rr_map_by_ticket())
    except Exception:
        pass
    return result


def _ticket_order_type_map_sync(days: int) -> dict[str, tuple[str, Optional[float]]]:
    """order_type/pending_placed_at aren't in the consolidated-ledger sync
    protocol yet, so a ticket the OTHER node opened falls back to
    "Market"/no pending time; local-only for now."""
    import time
    cutoff = time.time() - days * 86400
    result: dict[str, tuple[str, Optional[float]]] = {}
    try:
        rows = trade_history_repo.ticket_order_types(cutoff)
        for mt5_ticket, order_type, pending_placed_at in rows:
            result[str(mt5_ticket)] = (order_type or "market", pending_placed_at)
    except Exception:
        pass
    return result


def _ticket_group_map_sync() -> dict[str, tuple[str, int]]:
    """{mt5_ticket_str: (trade_id, tier)} for Adaptive Runner ladder legs,
    used to collapse a signal's N broker-side tickets into one table row."""
    result: dict[str, tuple[str, int]] = {}
    try:
        rows = trade_history_repo.ticket_groups()
        for trade_id, ticket, tier in rows:
            result[str(ticket)] = (trade_id, int(tier))
    except Exception:
        pass
    return result


def _ticket_info_sync() -> dict:
    """{ticket_str: (source_label, strategy_label, direction)} with the same
    cross-node consolidated-ledger fallback as the maps above."""
    info: dict[str, tuple] = {}
    try:
        ledger_channels, ledger_strategies, ledger_directions = db_module.get_consolidated_ticket_maps()
        for ticket, tg_source in ledger_channels.items():
            ch = trade_channel_label(tg_source or "")
            label = ch if ch else trade_source_label(tg_source or "")
            strat = ledger_strategies.get(ticket, "")
            info[ticket] = (
                label,
                strategy_display_label(strat or ""),
                ledger_directions.get(ticket, ""),
            )
    except Exception:
        pass
    try:
        rows = trade_history_repo.all_ticket_info()
        for tk, src, strat, dir_ in rows:
            ch = trade_channel_label(src or "")
            label = ch if ch else trade_source_label(src or "")
            # Local data always wins where both exist.
            info[str(tk)] = (label, strategy_display_label(strat or ""), dir_ or "")
    except Exception:
        pass
    try:
        rows = trade_history_repo.all_ticket_info_for_legs()
        for tk, src, strat, dir_ in rows:
            ch = trade_channel_label(src or "")
            label = ch if ch else trade_source_label(src or "")
            info[str(tk)] = (label, strategy_display_label(strat or ""), dir_ or "")
    except Exception:
        pass
    return info


async def ticket_source_map(days: int) -> dict[str, str]:
    return await db_module.to_db_thread(_ticket_source_map_sync, days)


async def ticket_strategy_map(days: int) -> dict[str, str]:
    return await db_module.to_db_thread(_ticket_strategy_map_sync, days)


async def ticket_max_tp_map() -> dict[str, str]:
    return await db_module.to_db_thread(_ticket_max_tp_map_sync)


async def ticket_rr_map() -> dict[str, float]:
    return await db_module.to_db_thread(_ticket_rr_map_sync)


async def ticket_order_type_map(days: int) -> dict[str, tuple[str, Optional[float]]]:
    return await db_module.to_db_thread(_ticket_order_type_map_sync, days)


async def ticket_group_map() -> dict[str, tuple[str, int]]:
    return await db_module.to_db_thread(_ticket_group_map_sync)


async def ticket_info() -> dict:
    return await db_module.to_db_thread(_ticket_info_sync)


async def run_db(fn, *args, **kwargs):
    """Off-loop dispatch for the page's remaining one-off reads."""
    return await db_module.to_db_thread(fn, *args, **kwargs)


async def get_cached_spreads(tickets: list) -> dict:
    return await db_module.to_db_thread(db_module.get_cached_spreads, tickets)


async def cache_spread(ticket, price, points, cost) -> None:
    return await db_module.to_db_thread(
        db_module.cache_spread, ticket, price, points, cost)


def get_hourly_pnl_grid(days: int):
    return db_module.get_hourly_pnl_grid(days)


def session_for_hour(h: int) -> str:
    return db_module._session_for_hour(h)


def get_app_config(key: str):
    return db_module.get_app_config(key)


def set_app_config(key: str, value: str) -> None:
    db_module.set_app_config(key, value)


def recompute_channel_performance(days: int):
    return db_module.recompute_channel_performance(days)


def get_channel_scorecard(days: int):
    return db_module.get_channel_scorecard(days)


def get_channel_performance_map():
    return db_module.get_channel_performance_map()


def set_channel_paused(source: str, paused) -> None:
    db_module.set_channel_paused(source, paused)
