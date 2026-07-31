"""SELECT-only queries backing the trade-history views.

Every statement here was inline in `frontend/pages/history.py`, which is how a
NiceGUI page ended up owning multi-table joins against `vantage_ladder_legs`.
This is step 1 of draining that page: the queries move, the shaping stays where
it is, so the diff on the page is "expression moved" and nothing else.

Two things kept deliberately:

* **Rows out, not dicts.** These are the exact tuples the page already
  destructures. Converting them here would change the page's code in the same
  commit that moves the SQL, and then a behaviour change would be
  indistinguishable from a relocation. The dict conversion belongs in the
  controller step.
* **The `open_time >= cutoff` predicate.** Every history view is windowed, and
  dropping the bound would turn a bounded scan into a full-table one on a
  database that grows forever.

The ladder-leg pairs need explaining, because it is not obvious why nearly every
query appears twice. Legs 2+ of a laddered trade have a `vantage_ladder_legs`
row but no `vantage_simulated_trades` row of their own, so a query against
trades alone silently omits them. The `_for_legs` variants join back to the
parent trade to recover the attribution the leg never had.
"""
from __future__ import annotations

from typing import Any, Optional

from backend.src.db import database as db_module


def _rows(sql: str, params: tuple = ()) -> list[tuple]:
    with db_module.db() as conn:
        return conn.execute(sql, params).fetchall()


# ── Channel attribution ───────────────────────────────────────────────────────

def ticket_sources(cutoff: float) -> list[tuple[Any, Any]]:
    """(mt5_ticket, tg_source) for trades opened since `cutoff`."""
    return _rows(
        "SELECT mt5_ticket, tg_source FROM vantage_simulated_trades "
        "WHERE mt5_ticket IS NOT NULL AND open_time >= ?",
        (cutoff,),
    )


def ticket_sources_for_legs(cutoff: float) -> list[tuple[Any, Any]]:
    """(mt5_ticket, tg_source) for ladder legs, inherited from the parent trade."""
    return _rows(
        "SELECT l.mt5_ticket, t.tg_source FROM vantage_ladder_legs l "
        "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
        "WHERE l.mt5_ticket IS NOT NULL AND t.open_time >= ?",
        (cutoff,),
    )


# ── Strategy attribution ──────────────────────────────────────────────────────

def ticket_strategies(cutoff: float) -> list[tuple[Any, Any, Any]]:
    """(mt5_ticket, strategy, dpm_trade_id) since `cutoff`.

    LEFT JOIN, not INNER: a trade with no dpm_trade_performance row must still
    appear, carrying its own strategy. An inner join would silently drop every
    non-DPM trade from the history view.
    """
    return _rows(
        "SELECT t.mt5_ticket, t.strategy, d.trade_id "
        "FROM vantage_simulated_trades t "
        "LEFT JOIN dpm_trade_performance d ON t.trade_id = d.trade_id "
        "WHERE t.mt5_ticket IS NOT NULL AND t.open_time >= ?",
        (cutoff,),
    )


def ticket_strategies_for_legs(cutoff: float) -> list[tuple[Any, Any]]:
    """(mt5_ticket, strategy) for ladder legs, inherited from the parent."""
    return _rows(
        "SELECT l.mt5_ticket, t.strategy FROM vantage_ladder_legs l "
        "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
        "WHERE l.mt5_ticket IS NOT NULL AND t.open_time >= ?",
        (cutoff,),
    )


# ── Order type ────────────────────────────────────────────────────────────────

def ticket_order_types(cutoff: float) -> list[tuple[Any, Any, Optional[float]]]:
    """(mt5_ticket, order_type, pending_placed_at) since `cutoff`."""
    return _rows(
        "SELECT mt5_ticket, order_type, pending_placed_at FROM vantage_simulated_trades "
        "WHERE mt5_ticket IS NOT NULL AND open_time >= ?",
        (cutoff,),
    )


# ── Ladder grouping ───────────────────────────────────────────────────────────

def ticket_groups() -> list[tuple[Any, Any, Any]]:
    """(trade_id, ticket, tier) across parent trades and their ladder legs.

    UNION ALL rather than UNION: the two halves are disjoint by construction --
    parents come from vantage_simulated_trades, legs from vantage_ladder_legs --
    so deduplicating would only cost a sort over the whole result.

    The parent half is tier 1 by definition; only trades that actually have legs
    are included, via the IN subquery.
    """
    return _rows(
        "SELECT t.trade_id, t.mt5_ticket AS ticket, 1 AS tier "
        "FROM vantage_simulated_trades t "
        "WHERE t.trade_id IN (SELECT DISTINCT trade_id FROM vantage_ladder_legs) "
        "AND t.mt5_ticket IS NOT NULL "
        "UNION ALL "
        "SELECT l.trade_id, l.mt5_ticket AS ticket, l.tier "
        "FROM vantage_ladder_legs l WHERE l.mt5_ticket IS NOT NULL"
    )


# ── Full ticket info (unwindowed) ─────────────────────────────────────────────
#
# These two have no cutoff on purpose: they back the consolidated ledger view,
# which reconciles against tickets of any age. Adding a window here would make
# older consolidated rows lose their channel and strategy labels.

def all_ticket_info() -> list[tuple[Any, Any, Any, Any]]:
    """(mt5_ticket, tg_source, strategy, direction) for every ticketed trade."""
    return _rows(
        "SELECT mt5_ticket, tg_source, strategy, direction "
        "FROM vantage_simulated_trades WHERE mt5_ticket IS NOT NULL"
    )


def all_ticket_info_for_legs() -> list[tuple[Any, Any, Any, Any]]:
    """(mt5_ticket, tg_source, strategy, direction) for every ticketed leg."""
    return _rows(
        "SELECT l.mt5_ticket, t.tg_source, t.strategy, t.direction "
        "FROM vantage_ladder_legs l "
        "JOIN vantage_simulated_trades t ON t.trade_id = l.trade_id "
        "WHERE l.mt5_ticket IS NOT NULL"
    )
