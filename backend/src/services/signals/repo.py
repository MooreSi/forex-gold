"""Signal CRUD -- extracted verbatim (no logic changes) from core/engine.py's
SimulationEngine.create_signal/get_signals/activate_signal/cancel_signal, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-signal-crud-migration/020-*.md.

None of these four functions ever used `self` in the original -- they extract
cleanly as plain functions taking explicit parameters, calling the shared
forex_trader.core.database module (db_module) directly. No parallel repo
needed: db_module already provides real transactional semantics
(thread-local, re-entrant db()).
"""
from __future__ import annotations

import json
import time
import uuid
from typing import Optional

from forex_trader.core import database as db_module
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
        if status:
            rows = conn.execute(
                "SELECT * FROM vantage_signals WHERE status=? ORDER BY created_at DESC", (status,)
            ).fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM vantage_signals ORDER BY created_at DESC"
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
