"""Data access for the Telegram control panel.

Moved out of core_bot_panel.py so the SQL sits in the data layer -- SQL is
allowed in `backend/src/db/`, `backend/migrations/` and `*_repo.py`, and
nowhere else. Statements are unchanged.

The two stop-loss writes are the reason this file is worth reading carefully.
Both are called only AFTER `bridge.modify_order` has accepted the new stop:
recording one the broker refused leaves the app believing a position is
protected at a price that was never set, which stays invisible until the market
reaches it. The ordering lives with the callers in core_bot_panel; these
functions do the write and nothing else.
"""
from __future__ import annotations

from backend.src.db.database import db, row_to_dict


def open_trades_for_sources(variants: list[str]) -> list[dict]:
    """Open trades for any of a channel's stored source spellings, oldest
    first. A channel can appear under several tg_source strings, which is why
    this takes a list rather than a name."""
    marks = ",".join("?" for _ in variants)
    with db() as conn:
        rows = conn.execute(
            f"SELECT * FROM vantage_simulated_trades "
            f"WHERE status='open' AND tg_source IN ({marks}) ORDER BY open_time",
            variants,
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def working_pending_orders_for_sources(variants: list[str]) -> list[dict]:
    """Pending orders still resting at the broker for a channel."""
    marks = ",".join("?" for _ in variants)
    with db() as conn:
        rows = conn.execute(
            f"SELECT p.* FROM vantage_pending_orders p "
            f"JOIN vantage_signals s ON s.signal_id = p.signal_id "
            f"WHERE p.status='working' AND s.source_name IN ({marks})",
            variants,
        ).fetchall()
    return [row_to_dict(r) for r in rows]


def open_trade_by_prefix(trade_prefix: str) -> dict | None:
    """One open trade addressed by the first characters of its id.

    The panel's callback_data cannot carry a full trade id -- Telegram caps the
    whole payload at 64 bytes -- so buttons carry a prefix and it resolves here.
    """
    with db() as conn:
        row = conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id LIKE ? AND status='open'",
            (trade_prefix + "%",),
        ).fetchone()
    return row_to_dict(row) if row else None


def record_stop_loss(trade_id: str, stop_loss: float) -> None:
    """Record a stop the broker has ALREADY accepted.

    Never call this before `modify_order` returns without an error. The app
    believing a stop it does not have is worse than showing a stale one,
    because nothing surfaces the difference until price arrives.
    """
    with db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                     (stop_loss, trade_id))
