"""Signal CRUD -- extracted verbatim (no logic changes) from core/engine.py's
SimulationEngine.create_signal/get_signals/activate_signal/cancel_signal, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-signal-crud-migration/020-*.md.

None of these four functions ever used `self` in the original -- they extract
cleanly as plain functions taking explicit parameters, calling the shared
backend.src.db.database module (db_module) directly. No parallel repo
needed: db_module already provides real transactional semantics
(thread-local, re-entrant db()).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.signals.parser import validate_signal


def create_signal(source_name: str, direction: str, entry_low: float,
                  entry_high: float, stop_loss: float,
                  tp1=None, tp2=None, tp3=None, tp4=None, tp5=None,
                  tp6=None, tp7=None, tp8=None,
                  lot_size=None, risk_pct=None, notes: str = "") -> dict:
    rs     = db_module.get_risk_settings()
    errors = validate_signal(direction, entry_low, entry_high, stop_loss,
                             tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8)
    if errors:
        raise ValueError("; ".join(errors))
    if rs.get("require_at_least_tp1") and tp1 is None:
        raise ValueError("At least TP1 is required")

    signal_id = str(uuid.uuid4())[:16]
    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,lot_size,risk_pct,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, source_name, direction.upper(), entry_low, entry_high, stop_loss,
             tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8,
             lot_size, risk_pct, notes, "pending", time.time()),
        )
    return {"signal_id": signal_id, "status": "pending"}


def get_signals(status: Optional[str] = None) -> list[dict]:
    with db_module.db() as conn:
        # rowid DESC is the tie-break: created_at is float seconds, so two
        # signals created in the same tick share a value and ORDER BY created_at
        # alone falls back to rowid ASC -- returning the older one first and
        # breaking "newest first". rowid is monotonic with insertion, so
        # newest-inserted wins the tie.
        if status:
            rows = conn.execute(
                "SELECT * FROM vantage_signals WHERE status=? "
                "ORDER BY created_at DESC, rowid DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vantage_signals ORDER BY created_at DESC, rowid DESC"
            ).fetchall()
    result = [db_module.row_to_dict(r) for r in rows]
    for r in result:
        if r.get("claude_commentary"):
            try:
                r["claude_commentary"] = json.loads(r["claude_commentary"])
            except Exception:
                pass
    return result


def activate_signal(signal_id: str) -> None:
    with db_module.db() as conn:
        row = db_module.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone()
        )
    if not row:
        raise ValueError(f"Signal {signal_id} not found")
    if row["status"] not in ("pending",):
        raise ValueError(f"Signal is {row['status']}, cannot activate")
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='active', activated_at=? WHERE signal_id=?",
            (time.time(), signal_id),
        )


def cancel_signal(signal_id: str) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='cancelled', cancelled_at=? WHERE signal_id=?",
            (time.time(), signal_id),
        )


# ── Reads/writes collected from the scan/activation paths (M1 SQL sweep) ─────
# Verbatim statements; the callers run them from the same places in the same
# order they always did.

def get_signal(signal_id: str) -> dict:
    with db_module.db() as conn:
        return db_module.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone()
        )


def get_pending_signals_awaiting_zone_fill() -> list[dict]:
    """Pending signals with no genuine EA pending order currently resting.

    Excludes any signal with a 'working' row in vantage_pending_orders --
    without this, the moment price re-enters the same entry zone that order
    is watching, the generic market-fill watcher would race it and open a
    SECOND, duplicate trade before the real pending order has even filled.
    Once that order resolves, vantage_pending_orders.status stops being
    'working' and/or vantage_signals.status moves off 'pending', so the
    exclusion is only load-bearing during that narrow window.
    """
    with db_module.db() as conn:
        return [
            db_module.row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_signals WHERE status='pending' "
                "AND signal_id NOT IN ("
                "  SELECT signal_id FROM vantage_pending_orders WHERE status='working'"
                ") ORDER BY created_at ASC"
            ).fetchall()
        ]


def expire_signal(signal_id: str) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='expired' WHERE signal_id=?",
            (signal_id,),
        )


def mark_signal_activated(signal_id: str) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET status='activated' WHERE signal_id=?",
            (signal_id,),
        )


def find_open_trade_for_signal(signal_id: str):
    """The duplicate-activation guard: any open/pending trade for this signal."""
    with db_module.db() as conn:
        return conn.execute(
            "SELECT trade_id FROM vantage_simulated_trades "
            "WHERE signal_id=? AND status IN ('open','pending')",
            (signal_id,),
        ).fetchone()


def find_latest_open_trade_for_source(channel_name: str) -> Optional[dict]:
    """Most recent open trade attributed to a channel (direct or instant)."""
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT * FROM vantage_simulated_trades "
            "WHERE status='open' AND tg_source IN (?,?) "
            "ORDER BY open_time DESC LIMIT 1",
            (channel_name, f"instant:{channel_name}"),
        ).fetchone()
        return db_module.row_to_dict(row) if row else None


def template_trade_open_entries(channel_name: str, direction: str,
                               strategy_like: str) -> list:
    """Sig Guard's question: the entry price of every template-managed trade
    already open on this channel and direction, newest-agnostic.

    Returns entries rather than a bare boolean because Sig Guard gained a
    distance arm on 2026-08-04 (upstream `guard_pips`): the caller decides
    whether an existing trade is close enough to the new one to block. An
    unfilled placeholder row has entry 0 and no price to measure from -- it
    is returned as-is and the caller treats it as blocking."""
    with db_module.db() as conn:
        rows = conn.execute(
            "SELECT entry_price FROM vantage_simulated_trades WHERE status='open' "
            "AND tg_source=? AND direction=? AND strategy LIKE ?",
            (channel_name, direction, strategy_like),
        ).fetchall()
    return [float(r[0] or 0) for r in rows]


def set_signal_commentary(signal_id: str, commentary_json: str) -> None:
    with db_module.db() as conn:
        conn.execute(
            "UPDATE vantage_signals SET claude_commentary=? WHERE signal_id=?",
            (commentary_json, signal_id),
        )
