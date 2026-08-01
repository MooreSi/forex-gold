"""Backtest's main-DB reads, collected out of engine.py (M1 SQL sweep).
Verbatim statements."""
from __future__ import annotations

from backend.src.db import database as db_module


def fetch_backtest_signals(live_trades_only: bool) -> list:
    with db_module.db() as conn:
        if live_trades_only:
            return conn.execute(
                "SELECT vs.signal_id, vs.direction, vs.entry_low, vs.entry_high, "
                "vs.stop_loss, vs.tp1, vs.tp2, vs.tp3, vs.created_at, vs.source_name, "
                "vs.tp4, vs.tp5, vs.tp6, vs.tp7, vs.tp8 "
                "FROM vantage_signals vs "
                "INNER JOIN vantage_simulated_trades vst ON vst.signal_id = vs.signal_id "
                "GROUP BY vs.signal_id ORDER BY vs.created_at"
            ).fetchall()
        return conn.execute(
            "SELECT signal_id, direction, entry_low, entry_high, stop_loss, "
            "tp1, tp2, tp3, created_at, source_name, tp4, tp5, tp6, tp7, tp8 "
            "FROM vantage_signals ORDER BY created_at"
        ).fetchall()
