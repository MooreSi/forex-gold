"""The trading service's SQL, collected out of its sixteen modules (M1 SQL
sweep). Verbatim statements; the callers run them from the same places in the
same order they always did.

This is the shared write-kernel for vantage_simulated_trades /
vantage_signals / vantage_partial_closes / vantage_pending_orders /
vantage_simulation_account that the refactor plan proposed: analytics keeps
its own SELECT-only read repo, and this file owns the order-lifecycle writes.

Multi-statement blocks that were one db() block inline are one transaction()
function here -- both rows land or neither does, exactly as before (db() at
depth 1 always committed the whole block).
"""
from __future__ import annotations

import logging

from backend.src.db import transaction
from backend.src.db import database as db_module
from backend.src.db.database import db, row_to_dict

log = logging.getLogger(__name__)


# ── vantage_simulated_trades: reads ──────────────────────────────────────────

def count_open_trades() -> int:
    with db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open'"
        ).fetchone()[0]


def get_trade(trade_id: str) -> dict:
    with db() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def get_open_trade_for_signal(signal_id: str) -> dict:
    with db() as conn:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE signal_id=? AND status='open'",
                (signal_id,),
            ).fetchone()
        )


def find_latest_instant_trade(channel_name: str, open_since: float):
    """Most recent open trade from a channel (direct or instant-prefixed) that
    opened at or after `open_since` -- one that could still plausibly be
    waiting for its follow-up signal.

    `open_since` is required, not defaulted: unbounded, this swallowed an
    unrelated signal 28 minutes later as a "follow-up" (2026-08-28 live miss,
    tg_id 19832). `tp1 IS NULL` is NOT an adequate substitute -- see
    docs/system/domains/trading/README.md.
    """
    with db() as conn:
        return conn.execute(
            "SELECT * FROM vantage_simulated_trades "
            "WHERE status='open' AND tg_source IN (?,?) AND open_time >= ? "
            "ORDER BY open_time DESC LIMIT 1",
            (channel_name, f"instant:{channel_name}", open_since),
        ).fetchone()


def find_channel_open_trade(channel_name: str):
    """Same tg_source matching convention as telegram/repo.py's copy -- direct
    name, instant-prefixed, or the "Telegram Auto (...)" wrapper."""
    with db() as conn:
        return conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE status='open' AND "
            "(tg_source=? OR tg_source=? OR tg_source LIKE ?) "
            "ORDER BY open_time DESC LIMIT 1",
            (channel_name, f"instant:{channel_name}", f"Telegram Auto ({channel_name})"),
        ).fetchone()


def fetch_open_trades_missing_tp1(cutoff: float) -> list:
    """IME watchdog: opens still waiting for a follow-up after the timeout."""
    with db() as conn:
        return conn.execute(
            """SELECT * FROM vantage_simulated_trades
               WHERE status='open' AND tp1 IS NULL AND open_time < ?""",
            (cutoff,),
        ).fetchall()


def fetch_closed_trades_since(day_cutoff: float) -> list[dict]:
    with db() as conn:
        return [
            row_to_dict(r)
            for r in conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE status='closed' AND close_time>? ORDER BY close_time DESC",
                (day_cutoff,),
            ).fetchall()
        ]


# ── vantage_simulated_trades: writes ─────────────────────────────────────────

def insert_trade_and_activate_signal(
    trade_id: str, signal_id, mt5_ticket, direction: str,
    entry_low, entry_high, entry_price, lot_size, stop_loss,
    tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8,
    now: float, strategy: str, tg_source, managed_by,
    grid_legs_total=None, initial_sl=None, initial_risk=None,
) -> None:
    """Open-trade row + signal activation, in one transaction.

    tp1..tp8 are the levels the trade is ACTUALLY running -- for an EA
    Template the template's resolved ladder, not the signal's own -- and a
    level the ladder does not define is passed as None, so a 6-level template
    stops claiming 8. grid_legs_total / initial_sl / initial_risk are the
    realised-R seed: neither the stop nor the lot size survives the trade
    (every breakeven path overwrites stop_loss in place, and lot_size only
    describes one leg of a grid), so both are captured at open. profit_sync
    later replaces initial_risk with the figure from the legs that actually
    filled -- a resting pending leg staged here may expire never taking on
    risk. Added by the upstream merge (2026-08-25, upstream 8d20bd3)."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                lot_size,remaining_lots,stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                status,open_time,spread_cost,commission,slippage_cost,net_pnl,strategy,tg_source,
                managed_by,grid_legs_total,initial_sl,initial_risk)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, entry_price,
             lot_size, lot_size, stop_loss, tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8,
             "open", now,
             0.0, 0.0, 0.0, 0.0,
             strategy, tg_source, managed_by,
             grid_legs_total, initial_sl, initial_risk),
        )
        conn.execute(
            "UPDATE vantage_signals SET status='active' WHERE signal_id=?", (signal_id,)
        )


def set_stop_loss(trade_id: str, new_sl: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
            (new_sl, trade_id),
        )


def set_trade_levels(trade_id: str, stop_loss, tp1=None, tp2=None, tp3=None,
                     tp4=None, tp5=None, tp6=None, tp7=None, tp8=None) -> None:
    """Post-fill SL/TP override. One canonical statement replaces the several
    inline variants that spelled unused TPs as literal NULLs -- binding None
    produces exactly the same NULL."""
    with db() as conn:
        conn.execute(
            """UPDATE vantage_simulated_trades
               SET stop_loss=?, tp1=?, tp2=?, tp3=?, tp4=?, tp5=?,
                   tp6=?, tp7=?, tp8=?
               WHERE trade_id=?""",
            (stop_loss, tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8, trade_id),
        )


def set_trade_strategy(trade_id: str, strategy: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET strategy=? WHERE trade_id=?",
            (strategy, trade_id),
        )


def update_trade_fields(trade_id: str, fields: dict) -> None:
    """Dynamic column update used by the follow-up paths. Column names come
    from fixed literal sets at the call sites, never from user input."""
    with db() as conn:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE vantage_simulated_trades SET {set_clause} WHERE trade_id=?",
            list(fields.values()) + [trade_id],
        )


def apply_followup_watchdog_levels(trade_id: str, tp_vals6: tuple, new_sl,
                                   sl_moved_db) -> None:
    with db() as conn:
        conn.execute(
            """UPDATE vantage_simulated_trades
               SET tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?,
                   stop_loss=?, sl_moved_to_be=?
               WHERE trade_id=?""",
            (*tp_vals6, new_sl, sl_moved_db, trade_id),
        )


# ── Close paths (each was one atomic db() block inline) ──────────────────────

def apply_full_close(trade_id: str, now: float, close_price: float, reason: str,
                     gross_pnl: float, realised_total: float, net_pnl_total: float,
                     net_delta: float, signal_id) -> None:
    with transaction() as conn:
        conn.execute(
            """UPDATE vantage_simulated_trades
               SET status=?,close_time=?,close_price=?,exit_reason=?,
                   gross_pnl=?,realised_pnl=?,swap_est=?,net_pnl=?,remaining_lots=0
               WHERE trade_id=?""",
            ("closed", now, close_price, reason,
             gross_pnl, realised_total,
             0.0, net_pnl_total, trade_id),
        )
        conn.execute(
            "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
            (net_delta,),
        )
        conn.execute(
            "UPDATE vantage_signals SET status='closed' WHERE signal_id=?",
            (signal_id,),
        )


def apply_ladder_close(trade_id: str, closed_any: bool, lots_closed: float,
                       last_price: float, total_pnl: float, reason: str,
                       signal_id, now: float) -> None:
    with transaction() as conn:
        if closed_any:
            conn.execute(
                "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,"
                "close_price,pnl,reason) VALUES (?,?,?,?,?,?)",
                (trade_id, now, lots_closed, last_price, total_pnl, reason),
            )
            conn.execute(
                "UPDATE vantage_simulation_account SET balance=balance+? WHERE id=1",
                (total_pnl,),
            )
        conn.execute(
            """UPDATE vantage_simulated_trades
               SET status='closed',close_time=?,close_price=?,exit_reason=?,
                   realised_pnl=realised_pnl+?,net_pnl=net_pnl+?,remaining_lots=0
               WHERE trade_id=?""",
            (now, round(last_price, 2), reason, total_pnl, total_pnl, trade_id),
        )
        conn.execute(
            "UPDATE vantage_signals SET status='closed' WHERE signal_id=?",
            (signal_id,),
        )


def apply_partial_close_with_reason(trade_id: str, now: float, lots_to_close: float,
                                    close_price: float, partial_pnl: float,
                                    new_remaining: float, entry_price: float,
                                    row: dict, reason: str) -> None:
    """partial_close.py's exact block: reason is parameterised and the TP1
    breakeven move consults risk settings inside the same transaction."""
    with transaction() as conn:
        conn.execute(
            "INSERT INTO vantage_partial_closes (trade_id,ts,lots_closed,close_price,pnl,reason)"
            " VALUES (?,?,?,?,?,?)",
            (trade_id, now, lots_to_close, close_price, partial_pnl, reason),
        )
        conn.execute(
            "UPDATE vantage_simulated_trades SET remaining_lots=?,realised_pnl=realised_pnl+?,"
            "net_pnl=net_pnl+? WHERE trade_id=?",
            (new_remaining, partial_pnl, partial_pnl, trade_id),
        )
        conn.execute(
            "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
            (partial_pnl,),
        )
        rs = db_module.get_risk_settings()
        if reason == "TP1" and rs.get("move_sl_to_be_after_tp1", 1) and not row["sl_moved_to_be"]:
            conn.execute(
                "UPDATE vantage_simulated_trades SET stop_loss=?,sl_moved_to_be=1 WHERE trade_id=?",
                (entry_price, trade_id),
            )
        if new_remaining <= 0:
            conn.execute(
                "UPDATE vantage_simulated_trades SET status='closed',close_time=?,"
                "exit_reason='all_tps_hit',close_price=?,remaining_lots=0 WHERE trade_id=?",
                (now, round(close_price, 2), trade_id),
            )
            conn.execute(
                "UPDATE vantage_signals SET status='closed' WHERE signal_id=?",
                (row["signal_id"],),
            )


# ── Profit sync ──────────────────────────────────────────────────────────────

def apply_profit_sync(trade_id: str, mt5_profit: float) -> None:
    """First-time sync corrects the simulated balance against the real MT5
    figure; every sync stamps mt5_profit. One transaction, as inline."""
    with transaction() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT mt5_profit, net_pnl FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone())
        if existing and existing.get("mt5_profit") is None:
            our_estimate = float(existing.get("net_pnl") or 0)
            correction = round(mt5_profit - our_estimate, 4)
            if abs(correction) >= 0.01:
                conn.execute(
                    "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
                    (correction,),
                )
                conn.execute(
                    "UPDATE vantage_simulated_trades SET net_pnl=? WHERE trade_id=?",
                    (mt5_profit, trade_id),
                )
                log.info(
                    "[ProfitSync] Balance corrected for %s: estimated=%.2f mt5=%.2f adj=%.2f",
                    trade_id, our_estimate, mt5_profit, correction,
                )
        conn.execute(
            "UPDATE vantage_simulated_trades SET mt5_profit=? WHERE trade_id=?",
            (mt5_profit, trade_id),
        )


def fetch_mt5_profit(trade_id: str):
    with db() as conn:
        return conn.execute(
            "SELECT mt5_profit FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()


def fetch_unsynced_closed(cutoff: float) -> list:
    with db() as conn:
        return conn.execute(
            """SELECT trade_id,mt5_ticket FROM vantage_simulated_trades
               WHERE status='closed' AND mt5_ticket IS NOT NULL
                 AND (mt5_profit IS NULL OR (mt5_profit=0.0 AND close_time>?))""",
            (cutoff,),
        ).fetchall()


# ── vantage_simulation_account ───────────────────────────────────────────────

def get_simulation_account() -> dict:
    with db() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM vantage_simulation_account WHERE id=1").fetchone()
        )


def add_to_sim_balance(delta: float) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
            (delta,),
        )


def reset_simulation_data(starting_balance: float, now: float) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE vantage_simulation_account SET balance=?, reset_at=? WHERE id=1",
            (starting_balance, now),
        )
        conn.execute("DELETE FROM vantage_simulated_trades")
        conn.execute("DELETE FROM vantage_partial_closes")
        conn.execute(
            "UPDATE vantage_signals SET status='cancelled' WHERE status IN ('pending','active')"
        )


# ── vantage_signals: activation lifecycle ────────────────────────────────────

def claim_signal_activation(signal_id: str) -> int:
    """Atomic claim: only one caller flips pending/active -> activating."""
    with db() as conn:
        return conn.execute(
            "UPDATE vantage_signals SET status='activating' "
            "WHERE signal_id=? AND status IN ('pending','active')",
            (signal_id,),
        ).rowcount


from backend.src.services.trading.signal_state_repo import (  # noqa: E402
    restore_signal_after_failed_open, reset_signal_to_pending,
    park_signal_pending, park_signal_unknown,
)


def update_signal_fields(signal_id: str, fields: dict) -> dict:
    """update_signal.py's block: dynamic column update + fetch of the open
    trade for the same signal, on the same connection."""
    with db() as conn:
        set_clause = ", ".join(f"{k}=?" for k in fields)
        conn.execute(
            f"UPDATE vantage_signals SET {set_clause} WHERE signal_id=?",
            list(fields.values()) + [signal_id],
        )
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vantage_simulated_trades "
                "WHERE signal_id=? AND status='open'",
                (signal_id,),
            ).fetchone()
        )


# ── vantage_signals + vantage_tg_signals: the auto-execution inserts ─────────

def insert_scan_signal(signal_id: str, source_name: str, parsed: dict, lot,
                       notes: str, status: str, created_at: float,
                       activated_at, tg_id, tg_status: str) -> None:
    """scan_auto_execute's queued/active pair -- one INSERT shape covers both;
    binding activated_at=None is identical to omitting the column."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                lot_size,notes,status,created_at,activated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, source_name,
             parsed["direction"], parsed["entry_low"], parsed["entry_high"],
             parsed["stop_loss"],
             parsed["tp1"], parsed["tp2"], parsed["tp3"], parsed["tp4"], parsed["tp5"],
             parsed.get("tp6"), parsed.get("tp7"), parsed.get("tp8"),
             lot, notes, status, created_at, activated_at),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status=?,signal_id=?"
            " WHERE tg_message_id=?",
            (tg_status, signal_id, tg_id),
        )


def insert_bot_signal(signal_id: str, tg_sig: dict, created_at: float) -> None:
    """bot_trading's /activate: signal from the newest unactivated tg row."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "Telegram Bot", tg_sig["direction"],
             tg_sig["entry_low"], tg_sig["entry_high"], tg_sig["stop_loss"],
             tg_sig["tp1"], tg_sig["tp2"], tg_sig["tp3"], tg_sig["tp4"], tg_sig["tp5"],
             tg_sig.get("tp6"), tg_sig.get("tp7"), tg_sig.get("tp8"),
             f"Bot-activated from Telegram {tg_sig['tg_message_id']}",
             "active", created_at),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='activated',signal_id=? WHERE tg_message_id=?",
            (signal_id, tg_sig["tg_message_id"]),
        )


def fetch_newest_unactivated_tg_signal() -> dict:
    with db() as conn:
        return row_to_dict(conn.execute(
            "SELECT * FROM vantage_tg_signals WHERE status='new' ORDER BY parsed_at DESC LIMIT 1"
        ).fetchone())


def insert_instant_signal(signal_id: str, channel_name: str, direction: str,
                          entry_px: float, provisional_sl: float, lot,
                          notes: str, now: float, tg_id) -> None:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                lot_size,notes,status,created_at,activated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, f"Telegram Instant ({channel_name})",
             direction, entry_px, entry_px, provisional_sl,
             None, None, None, None, None, None, None, None,
             lot, notes, "active", now, now),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='instant_activated', signal_id=?"
            " WHERE tg_message_id=?",
            (signal_id, tg_id),
        )


def delete_failed_instant_signal(signal_id: str, tg_id) -> None:
    with transaction() as conn:
        conn.execute("DELETE FROM vantage_signals WHERE signal_id=?", (signal_id,))
        conn.execute(
            "UPDATE vantage_tg_signals SET status='instant_failed'"
            " WHERE tg_message_id=?",
            (tg_id,),
        )


def set_tg_followup_applied(tg_id, signal_id) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_tg_signals SET status='followup_applied', signal_id=?"
            " WHERE tg_message_id=?",
            (signal_id, tg_id),
        )


# ── vantage_pending_orders: limit/ORB pending placements ────────────────────

def insert_pending_order_signal(signal_id: str, source_name: str, direction: str,
                                entry_low, entry_high, stop_loss,
                                tps: dict, lot, notes: str, now: float,
                                tg_update, pending_row: tuple) -> None:
    """A pending-order placement: signal row + optional tg flip + the
    vantage_pending_orders row, atomically. `tg_update` is (status, tg_id) or
    None; `pending_row` is the full 16-value tuple for the orders table."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, source_name, direction, entry_low, entry_high,
             stop_loss, tps.get(1), tps.get(2), tps.get(3), tps.get(4), tps.get(5),
             tps.get(6), tps.get(7), tps.get(8), lot, notes,
             "pending", now),
        )
        if tg_update is not None:
            tg_status, tg_id = tg_update
            conn.execute(
                "UPDATE vantage_tg_signals SET status=?,signal_id=? WHERE tg_message_id=?",
                (tg_status, signal_id, tg_id),
            )
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            pending_row,
        )


def insert_realigned_limit_entry(signal_id: str, source_label: str, direction: str,
                                 entry_low, entry_high, realigned_sl,
                                 realigned_tps: dict, lot, notes: str,
                                 tg_id, trade_row: tuple, now: float) -> None:
    """Limit Runner's zone-breached fallback: signal + tg flip + an OPEN trade
    row (the EA already filled at market), atomically."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, f"Telegram Auto ({source_label})", direction, entry_low, entry_high,
             realigned_sl, realigned_tps.get(1), realigned_tps.get(2), realigned_tps.get(3),
             realigned_tps.get(4), realigned_tps.get(5), realigned_tps.get(6),
             realigned_tps.get(7), realigned_tps.get(8), lot, notes,
             "active", now),
        )
        conn.execute(
            "UPDATE vantage_tg_signals SET status='active',signal_id=? WHERE tg_message_id=?",
            (signal_id, tg_id),
        )
        conn.execute(
            """INSERT INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                lot_size,remaining_lots,stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                status,open_time,spread_cost,commission,slippage_cost,net_pnl,strategy,
                tg_source,managed_by,tp_open,order_type,pending_placed_at,
                initial_sl,initial_risk)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            trade_row,
        )


def insert_manual_market_signal(signal_id: str, direction: str, entry_px: float,
                                sl, take_profit, final_lot, now: float) -> None:
    with db() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,tp1,
                lot_size,notes,status,created_at,activated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "Manual Market Order",
             direction, entry_px, entry_px, sl, take_profit,
             final_lot,
             "Manual market order placed from dashboard",
             "active", now, now),
        )


def insert_orb_pending(signal_id: str, mt5_direction: str, entry_zone_low,
                       entry_zone_high, stop_loss, target, lot, notes: str,
                       now: float, pending_row: tuple) -> None:
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                tp1,lot_size,notes,status,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "ORB/IVB Report (auto)", mt5_direction,
             entry_zone_low, entry_zone_high, stop_loss, target, lot,
             notes, "pending", now),
        )
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            pending_row,
        )


# ── channel_unrecognised_messages: dedup check on the AI-fallback path ───────

def unrecognised_message_exists(tg_id) -> bool:
    with db() as conn:
        return conn.execute(
            "SELECT id FROM channel_unrecognised_messages WHERE tg_message_id=?",
            (tg_id,),
        ).fetchone() is not None


def insert_instant_tg_row(tg_id, group_id, channel_name: str, sender_name: str,
                          msg_ts_str, text: str, parsed_at: float,
                          direction, status: str) -> None:
    """IME's bare-direction park (instant_pending / instant_historical)."""
    with db() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO vantage_tg_signals
               (tg_message_id,group_id,group_name,sender_name,message_ts,raw_text,parsed_at,
                direction,entry_low,entry_high,stop_loss,
                tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (tg_id, group_id, channel_name, sender_name, msg_ts_str,
             text, parsed_at,
             direction, None, None, None,
             None, None, None, None, None, None, None, None,
             status),
        )


# ── M4: runtime.py SQL sweep ─────────────────────────────────────────────────

def reopen_residual_trade(trade_id: str, residual_vol: float) -> None:
    """all-TPs-hit found residual broker volume: flip the row back to open
    with the real remaining lots so the close path can finish the job."""
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET status='open',close_time=NULL,"
            "exit_reason=NULL,remaining_lots=? WHERE trade_id=?",
            (residual_vol, trade_id),
        )


def get_trade_mt5_ticket(trade_id: str) -> dict:
    with db() as conn:
        return row_to_dict(conn.execute(
            "SELECT mt5_ticket FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone())


def set_open_commentary(trade_id: str, commentary_json: str) -> None:
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET claude_open=? WHERE trade_id=?",
            (commentary_json, trade_id),
        )


# ── Profit sync ───────────────────────────────────────────────────────────────
#
# Moved out of services/trading/profit_sync.py so the SQL sits in the data
# layer. The statements and their order are unchanged; the reasoning that made
# them safe is repeated on apply_profit_sync because it is the part a future
# edit is most likely to break.

def fetch_trade_strategy_and_sl(trade_id: str) -> dict:
    """strategy / direction / initial_sl for one trade, or {}."""
    with db() as conn:
        row = conn.execute(
            "SELECT strategy, direction, initial_sl "
            "FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()
        return row_to_dict(row) if row else {}


def fetch_synced_profit(trade_id: str):
    """The mt5_profit stamp, or None while legs are still settling. The retry
    ladder in schedule_profit_sync stops on this being non-NULL."""
    with db() as conn:
        return conn.execute(
            "SELECT mt5_profit FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)
        ).fetchone()


def fetch_unsynced_closed_trades(close_time_from: float) -> list:
    """Closed trades whose broker P&L was never recorded, plus recent
    zero-profit rows -- a real zero and a failed sync look identical, so the
    sweep re-checks the recent ones."""
    with db() as conn:
        return conn.execute(
            """SELECT trade_id,mt5_ticket FROM vantage_simulated_trades
               WHERE status='closed' AND mt5_ticket IS NOT NULL
                 AND (mt5_profit IS NULL OR (mt5_profit=0.0 AND close_time>?))""",
            (close_time_from,),
        ).fetchall()


def apply_profit_sync(trade_id: str, mt5_profit: float, risk_total: float,
                      all_settled: bool) -> tuple[float, float] | None:
    """Correct the account balance and the trade row against the broker's
    numbers. Returns (our_estimate, correction) when a correction was applied,
    else None, so the caller can log it.

    One transaction, because the balance adjustment and the net_pnl it
    accounts for must land together.

    mt5_profit stays NULL until all_settled, so the first branch runs on every
    retry while sibling legs are still closing -- our_estimate is always what
    the LAST call wrote, so the correction is exactly the increment a
    newly-closed leg added, never the same money twice.

    initial_risk is an absolute, not an increment, so re-running it as later
    legs settle converges on the full basket instead of accumulating. It is
    written on every pass so the R:R matches whatever P&L the row currently
    shows, over the same set of legs.
    """
    applied = None
    with transaction() as conn:
        existing = row_to_dict(conn.execute(
            "SELECT mt5_profit, net_pnl FROM vantage_simulated_trades WHERE trade_id=?",
            (trade_id,),
        ).fetchone())
        if existing and existing.get("mt5_profit") is None:
            our_estimate = float(existing.get("net_pnl") or 0)
            correction = round(mt5_profit - our_estimate, 4)
            if abs(correction) >= 0.01:
                conn.execute(
                    "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
                    (correction,),
                )
                conn.execute(
                    "UPDATE vantage_simulated_trades SET net_pnl=? WHERE trade_id=?",
                    (mt5_profit, trade_id),
                )
                applied = (our_estimate, correction)
        if risk_total > 0:
            conn.execute(
                "UPDATE vantage_simulated_trades SET initial_risk=? WHERE trade_id=?",
                (round(risk_total, 4), trade_id),
            )
        if all_settled:
            conn.execute(
                "UPDATE vantage_simulated_trades SET mt5_profit=? WHERE trade_id=?",
                (mt5_profit, trade_id),
            )
    return applied


def apply_fixed_rr_levels(trade_id: str, stop_loss: float, take_profit: float) -> None:
    """Replace a Fixed R:R trade's SL and TP ladder with the two exact levels
    computed from the actual fill.

    tp2..tp8 are explicitly NULLed rather than left alone: the pre-fill proxy
    row may carry the signal's own ladder, and this strategy is deliberately
    unmanaged after open -- a stale tp2 would be a target nothing is watching.
    The caller pushes the same two levels to the broker; see open_from_signal
    for why its return value has to be checked rather than trusted.
    """
    with db() as conn:
        conn.execute(
            """UPDATE vantage_simulated_trades
               SET stop_loss=?, tp1=?, tp2=NULL, tp3=NULL, tp4=NULL,
                   tp5=NULL, tp6=NULL, tp7=NULL, tp8=NULL
               WHERE trade_id=?""",
            (stop_loss, take_profit, trade_id),
        )
