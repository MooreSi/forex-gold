"""The broker service's SQL, collected out of ea_bridge.py, ea_templates.py
and history_import.py (M1 SQL sweep). Verbatim statements; the callers run
them from the same places in the same order they always did.

The pending-order fill/cancel handlers were each one atomic db() block inline
and are one transaction() function here -- an EA fill either lands the trade
row, activates the signal AND resolves the pending order, or none of it.
"""
from __future__ import annotations

import logging

from backend.src.db import transaction
from backend.src.db.database import db, row_to_dict
from backend.src.utils.models import CONTRACT_SIZE

log = logging.getLogger(__name__)


# ── vantage_pending_orders / the EA fill lifecycle ───────────────────────────

def fetch_working_pending_orders() -> list[dict]:
    with db() as conn:
        return [
            row_to_dict(r) for r in conn.execute(
                "SELECT * FROM vantage_pending_orders WHERE status='working'"
            ).fetchall()
        ]


def fetch_pending_order(trade_id: str) -> dict:
    with db() as conn:
        return row_to_dict(
            conn.execute(
                "SELECT * FROM vantage_pending_orders WHERE trade_id=?", (trade_id,)
            ).fetchone()
        )


def fetch_trade(trade_id: str) -> dict:
    with db() as conn:
        return row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
        )


def set_stop_loss_be(trade_id: str, new_sl: float) -> None:
    """The EA reported it moved SL to breakeven -- mirror it in the DB."""
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades SET stop_loss=?, sl_moved_to_be=1 WHERE trade_id=?",
            (new_sl, trade_id),
        )


def apply_pending_fill(trade_id: str, row: dict, ticket, fill_price: float,
                       tps: dict, now: float) -> None:
    """A resting EA order filled: open the trade row, activate its signal,
    resolve the pending order -- atomically."""
    with transaction() as conn:
        conn.execute(
            """INSERT INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                lot_size,remaining_lots,stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                status,open_time,spread_cost,commission,slippage_cost,net_pnl,strategy,
                tg_source,managed_by,tp_open,order_type,pending_placed_at,
                initial_sl,initial_risk)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, row["signal_id"], ticket, row["direction"],
             row["price"], row["price"], fill_price,
             row["lot_size"], row["lot_size"], row["stop_loss"],
             tps.get("1"), tps.get("2"), tps.get("3"), tps.get("4"),
             tps.get("5"), tps.get("6"), tps.get("7"), tps.get("8"),
             "open", now, 0.0, 0.0, 0.0, 0.0, row["strategy"],
             # channel_name was known and stored at placement time
             # (core_limit_order_signal.py / orb_auto_execute) but was once
             # never carried over here -- confirmed live 2026-07-23 that every
             # Limit Runner fill lost its real channel attribution and showed
             # as an unattributed trade in Trade Analysis.
             row["channel_name"], "ea", row["tp_open"],
             "limit", row["created_at"],
             # Realised-R inputs, against the stop this fill actually opened
             # with. See the initial_sl/initial_risk migration note.
             row["stop_loss"],
             round(abs(fill_price - float(row["stop_loss"])) * float(row["lot_size"])
                   * CONTRACT_SIZE, 4) if row.get("stop_loss") else None),
        )
        conn.execute(
            "UPDATE vantage_signals SET status='active' WHERE signal_id=?",
            (row["signal_id"],),
        )
        conn.execute(
            "UPDATE vantage_pending_orders SET status='filled',resolved_at=? WHERE trade_id=?",
            (now, trade_id),
        )


def claim_template_leg_fill(original_id: str, ticket, fill_price: float,
                            lots, kind: str, now: float):
    """An EA Template leg went live: stamp the placeholder row (mt5_ticket=0)
    with the real ticket/fill, and say whether THIS leg was the one that
    promoted it.

    Returns (row, is_first). `row` is the promoted row when is_first is True,
    or the already-promoted row (fetched purely so the caller can report this
    leg's fill with the right channel/strategy/TP context) when False, or {}
    when no row exists yet. Nothing is written for a later leg: there is
    nowhere in the current schema to record a second concurrent position.

    `kind` is "a" for an anchor leg (a market fill) or "g" for a grid limit.
    Extended by the 2026-08-25 upstream merge to cover anchor legs, whose
    fills previously reached nothing at all -- the row kept
    mt5_ticket=0/entry_price=0 for life, so Active Trades showed a $0 entry,
    every EA-reported TP/SL/close event for it was discarded as an unknown
    trade_id, and the close message quoted ticket 0 with a P&L computed from
    a $0 entry (confirmed live 2026-07-29: -$16086 reported on a real
    -$15.63 loss).
    """
    with db() as conn:
        # row_to_dict(None) returns {} (falsy), not None -- this must test
        # truthiness, never `is not None`.
        row = row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=? AND mt5_ticket=0",
            (original_id,),
        ).fetchone())
        if row:
            conn.execute(
                "UPDATE vantage_simulated_trades SET mt5_ticket=?,entry_price=?,"
                "entry_low=?,entry_high=?,lot_size=?,remaining_lots=?,open_time=?,"
                "order_type=?,pending_placed_at=? WHERE trade_id=?",
                # row["open_time"] (read above, before this UPDATE overwrites
                # it) is when open_trade() placed the legs -- the only
                # placement timestamp a leg has, since template legs never get
                # their own vantage_pending_orders row.
                (ticket, fill_price, fill_price, fill_price,
                 lots if lots else row["lot_size"],
                 lots if lots else row["remaining_lots"],
                 now,
                 "market" if kind == "a" else "limit",
                 row["open_time"], original_id),
            )
            row = dict(row)
            row["mt5_ticket"]  = ticket
            row["entry_price"] = fill_price
            if lots:
                row["lot_size"] = lots
                row["remaining_lots"] = lots
            return row, True
        already = row_to_dict(conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE trade_id=?",
            (original_id,),
        ).fetchone())
        return already, False


def apply_pending_cancelled(trade_id: str, signal_id, now: float) -> None:
    with transaction() as conn:
        conn.execute(
            "UPDATE vantage_pending_orders SET status='cancelled',resolved_at=? WHERE trade_id=?",
            (now, trade_id),
        )
        conn.execute(
            "UPDATE vantage_signals SET status='cancelled',cancelled_at=? WHERE signal_id=?",
            (now, signal_id),
        )


# ── ea_trade_templates ───────────────────────────────────────────────────────

def fetch_ea_templates() -> list:
    with db() as conn:
        return conn.execute(
            "SELECT * FROM ea_trade_templates ORDER BY name COLLATE NOCASE"
        ).fetchall()


def fetch_ea_template(name: str):
    with db() as conn:
        return conn.execute(
            "SELECT * FROM ea_trade_templates WHERE name=?", (name,),
        ).fetchone()


def upsert_ea_template(name: str, clean: dict, now: float) -> None:
    """Insert-or-update, preserving the original created_at. Column names come
    from ea_templates.DEFAULTS -- a fixed literal set, never user input."""
    with db() as conn:
        existing = conn.execute(
            "SELECT created_at FROM ea_trade_templates WHERE name=?", (name,),
        ).fetchone()
        created_at = existing[0] if existing else now
        cols = list(clean.keys())
        conn.execute(
            f"INSERT INTO ea_trade_templates (name, {', '.join(cols)}, created_at, updated_at) "
            f"VALUES (?, {', '.join('?' for _ in cols)}, ?, ?) "
            f"ON CONFLICT(name) DO UPDATE SET "
            + ", ".join(f"{c}=excluded.{c}" for c in cols)
            + ", updated_at=excluded.updated_at",
            (name, *[clean[c] for c in cols], created_at, now),
        )


def delete_ea_template(name: str) -> None:
    with db() as conn:
        conn.execute("DELETE FROM ea_trade_templates WHERE name=?", (name,))


# ── MT5 history import ───────────────────────────────────────────────────────

def fetch_existing_mt5_tickets() -> set:
    with db() as conn:
        return {
            row[0] for row in conn.execute(
                "SELECT mt5_ticket FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
            ).fetchall()
        }


def insert_imported_trade(signal_id: str, trade_id: str, ticket, direction: str,
                          entry_price: float, open_ts, close_ts, close_price,
                          exit_reason, gross_pnl, mt5_profit, lot_size) -> None:
    """One imported MT5 position: closed signal + closed trade + balance,
    atomically -- exactly the inline block's statement set."""
    with transaction() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO vantage_signals
               (signal_id,source_name,direction,entry_low,entry_high,stop_loss,
                notes,status,created_at,activated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (signal_id, "MT5 Import", direction, entry_price, entry_price, 0.0,
             f"Imported from MT5 ticket {ticket}", "closed", open_ts, open_ts),
        )
        conn.execute(
            """INSERT OR IGNORE INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                lot_size,remaining_lots,stop_loss,status,open_time,close_time,
                close_price,exit_reason,gross_pnl,realised_pnl,net_pnl,mt5_profit,strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, ticket, direction, entry_price, entry_price, entry_price,
             lot_size, 0.0, 0.0, "closed", open_ts, close_ts,
             close_price, exit_reason, gross_pnl, gross_pnl, mt5_profit, mt5_profit,
             "MT5_import"),
        )
        conn.execute(
            "UPDATE vantage_simulation_account SET balance = balance + ? WHERE id=1",
            (mt5_profit,),
        )


# ── MT5 close-reconciliation reads/writes (M4: runtime.py SQL sweep) ─────────
# Verbatim from runtime.py's _sync_closed_mt5_positions; the reconciliation
# LOGIC (miss-streak, partial-close detection) stays in the engine -- only
# the statements moved.

def fetch_python_managed_open_trades() -> list[dict]:
    """Open, python-managed trades with a real ticket -- ladder legs excluded
    (they are tracked in vantage_ladder_legs, never as their own trade row)."""
    with db() as conn:
        return [row_to_dict(r) for r in conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE status='open' AND mt5_ticket IS NOT NULL "
            "AND (managed_by IS NULL OR managed_by != 'ea') "
            "AND trade_id NOT IN (SELECT DISTINCT trade_id FROM vantage_ladder_legs)"
        ).fetchall()]


def reassign_mt5_ticket(trade_id: str, new_ticket) -> None:
    """A broker-side partial close continues the position under a new ticket."""
    with db() as conn:
        conn.execute(
            "UPDATE vantage_simulated_trades "
            "SET mt5_ticket=? WHERE trade_id=?",
            (new_ticket, trade_id),
        )


def fetch_known_mt5_tickets() -> set:
    """Every ticket this node already tracks -- trade rows plus ladder legs
    (legs 2+ never get their own trade row; without them every non-anchor leg
    looked 'untracked' and was re-imported as a phantom duplicate)."""
    with db() as conn:
        known = {
            int(r[0])
            for r in conn.execute(
                "SELECT mt5_ticket FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
            ).fetchall()
        }
        known |= {
            int(r[0])
            for r in conn.execute(
                "SELECT mt5_ticket FROM vantage_ladder_legs WHERE mt5_ticket IS NOT NULL"
            ).fetchall()
        }
        return known


def import_direct_mt5_position(trade_id: str, ticket, direction: str,
                               entry_p: float, lot_size: float, sl, tp,
                               open_ts: float, strategy: str,
                               sentinel_ts: float,
                               initial_sl=None, initial_risk=None) -> None:
    """Import a position opened directly in MT5. The MT5_DIRECT sentinel
    signal row is ensured first (idempotent) -- signal_id is NOT NULL with a
    FK, so without the sentinel this insert can never succeed."""
    with transaction() as conn:
        conn.execute(
            """INSERT OR IGNORE INTO vantage_signals
               (signal_id, source_name, direction, entry_low, entry_high,
                stop_loss, status, created_at)
               VALUES ('MT5_DIRECT', 'MT5 direct import', ?, 0, 0, 0,
                       'activated', ?)""",
            (direction, sentinel_ts),
        )
        conn.execute(
            """INSERT INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,
                entry_price,lot_size,remaining_lots,stop_loss,tp1,
                status,open_time,strategy,tg_source,
                initial_sl,initial_risk)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                trade_id, "MT5_DIRECT", ticket, direction,
                entry_p, entry_p, entry_p,
                lot_size, lot_size,
                sl, tp,
                "open", open_ts, strategy, "MT5_imported",
                # Realised-R inputs (see the initial_sl/initial_risk migration
                # note). An imported position may carry no stop at all, which is
                # genuinely unmeasurable risk -- left NULL, never recorded as 0.
                initial_sl, initial_risk,
            ),
        )
