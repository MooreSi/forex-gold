"""Max Tp — split from core/database.py.
Extracted from forex_trader/core/database.py -- see
docs/todo/refactor/core-database-migration/. Verbatim port: same functions,
same SQL, same behavior, using database.py's own db()/to_db_thread()
machinery (unchanged, already correct -- this is a pure file-size split,
not a connection-layer migration). Re-exported from database.py so every
existing `db_module.<name>` call site works completely unchanged.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from forex_trader.core.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402


def save_max_tp_hit(trade_id: str, value: str) -> None:
    """Persist the max TP level reached during the trade's own open->close window."""
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET max_tp_hit=? WHERE trade_id=?",
            (value, trade_id),
        )


def get_trades_with_max_tp_set() -> list[dict]:
    """Return closed trades that already have max_tp_hit computed.

    Used by the one-off backfill (engine.py's _backfill_max_tp_hit_corrected,
    2026-07-18) that recomputes every existing value against the corrected
    open_time->close_time window, replacing values computed under the old
    close_time+30min window that could attribute post-close price action to
    a trade that had already closed."""
    with db() as conn:
        rows = conn.execute(
            "SELECT t.trade_id, t.direction, t.open_time, t.close_time, "
            "t.max_tp_hit AS old_hit, t.strategy, t.tg_source, t.mt5_ticket, t.net_pnl, "
            "t.tp1, t.tp2, t.tp3, t.tp4, t.tp5, t.tp6, t.tp7, t.tp8, "
            "s.tp1 AS sig_tp1, s.tp2 AS sig_tp2, s.tp3 AS sig_tp3, "
            "s.tp4 AS sig_tp4, s.tp5 AS sig_tp5, s.tp6 AS sig_tp6, "
            "s.tp7 AS sig_tp7, s.tp8 AS sig_tp8 "
            "FROM vantage_simulated_trades t "
            "LEFT JOIN vantage_signals s ON s.signal_id = t.signal_id "
            "WHERE t.status='closed' AND t.max_tp_hit IS NOT NULL "
            "  AND t.open_time IS NOT NULL AND t.close_time > 0"
        ).fetchall()
    return [dict(r) for r in rows]


def get_max_tp_map_by_ticket() -> dict[str, str]:
    """Return {mt5_ticket_str: max_tp_hit} for all trades that have been computed."""
    with db() as conn:
        rows = conn.execute(
            "SELECT mt5_ticket, max_tp_hit FROM vantage_simulated_trades "
            "WHERE mt5_ticket IS NOT NULL AND max_tp_hit IS NOT NULL"
        ).fetchall()
    return {str(r[0]): r[1] for r in rows}


def get_rr_map_by_ticket() -> dict[str, float]:
    """Return {mt5_ticket_str: realized R-multiple} -- the trade's actual
    net P&L relative to what was actually risked at entry (SL distance on
    the opening lot size), not a static TP1-vs-SL plan ratio computed once
    at signal time.

    The previous version used abs(tp1 - entry) / abs(entry - stop) for
    every closed trade regardless of strategy or outcome. For the several
    strategies here that ladder through TP2-TP8 (Signal Climber, Reversal
    Runner, Adaptive Runner/2, Conservative, Limit Runner...) that showed
    the same understated ratio whether the trade closed at TP1 or ran the
    full ladder to TP8 -- reported live 2026-07-24 as R:R "not being
    reported correctly" across Closed Trades. Realized R is the standard
    trade-journal definition of R:R for an already-closed trade: how many
    multiples of the initial risk did this trade actually return.

    Risk distance prefers vantage_signals.stop_loss (set once at signal
    creation, never touched again) over vantage_simulated_trades.stop_loss
    -- every breakeven/trailing-stop code path (be_runner, scale_out,
    protected_scale, scalp_runner, conservative_trial, DPM, TP safety net,
    the EA's own sl_moved reports...) overwrites the latter IN PLACE, so
    for any trade that had already banked enough to move to breakeven, its
    stored stop_loss no longer reflects what was actually risked -- often
    landing exactly on entry_price (zero risk, ratio undefined), silently
    blanking R:R for a large share of winning trades specifically. Falls
    back to the trade's own stop_loss when there's no linked signal (manual
    trades) or the signal's stop_loss is unset.

    Available immediately at close (net_pnl/entry_price/lot_size are all
    set by record_close()), no async job involved -- excluded whenever any
    input is missing or the resolved risk distance is zero."""
    from forex_trader.core.core_fees_sizing import pnl as _pnl
    with db() as conn:
        rows = conn.execute(
            "SELECT t.mt5_ticket, t.direction, t.entry_price, "
            "COALESCE(s.stop_loss, t.stop_loss) AS risk_stop, "
            "t.lot_size, t.net_pnl "
            "FROM vantage_simulated_trades t "
            "LEFT JOIN vantage_signals s ON s.signal_id = t.signal_id "
            "WHERE t.mt5_ticket IS NOT NULL AND t.direction IS NOT NULL "
            "AND t.entry_price IS NOT NULL "
            "AND t.lot_size IS NOT NULL AND t.net_pnl IS NOT NULL "
            "AND COALESCE(s.stop_loss, t.stop_loss) IS NOT NULL"
        ).fetchall()
    result: dict[str, float] = {}
    for mt5_ticket, direction, entry_price, risk_stop, lot_size, net_pnl in rows:
        risk_dollars = abs(_pnl(direction, float(entry_price), float(risk_stop), float(lot_size)))
        if risk_dollars <= 0:
            continue
        result[str(mt5_ticket)] = float(net_pnl) / risk_dollars
    return result


def get_trades_pending_max_tp(cutoff_ts: float) -> list[dict]:
    """Return closed trades whose 30-min window has elapsed but max_tp_hit is not set.

    Joins vantage_signals to return the original signal's TP ladder (sig_tp1..sig_tp8)
    alongside the trade's own TPs.  The caller should prefer signal TPs so the Max TP
    column reflects how far price ran relative to the original signal levels, regardless
    of which strategy SL/TP the trade was closed under.

    Also returns strategy/tg_source/mt5_ticket/net_pnl — not used for the
    max_tp computation itself, but needed by the caller's follow-up
    push_trade_closed() call so the consolidated-ledger upsert (the one that
    finally lets the OTHER node's History view show this trade's Max TP Hit)
    doesn't have to clobber those fields with placeholder values.
    """
    with db() as conn:
        rows = conn.execute(
            "SELECT t.trade_id, t.direction, t.open_time, t.close_time, "
            "t.strategy, t.tg_source, t.mt5_ticket, t.net_pnl, "
            "t.tp1, t.tp2, t.tp3, t.tp4, t.tp5, t.tp6, t.tp7, t.tp8, "
            "s.tp1 AS sig_tp1, s.tp2 AS sig_tp2, s.tp3 AS sig_tp3, "
            "s.tp4 AS sig_tp4, s.tp5 AS sig_tp5, s.tp6 AS sig_tp6, "
            "s.tp7 AS sig_tp7, s.tp8 AS sig_tp8 "
            "FROM vantage_simulated_trades t "
            "LEFT JOIN vantage_signals s ON s.signal_id = t.signal_id "
            "WHERE t.status='closed' AND t.max_tp_hit IS NULL "
            "  AND t.close_time > 0 AND t.close_time <= ? "
            "  AND (t.tp1 IS NOT NULL OR s.tp1 IS NOT NULL)",
            (cutoff_ts,),
        ).fetchall()
    return [dict(r) for r in rows]
