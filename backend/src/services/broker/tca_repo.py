"""Storage for measured execution cost.

The SQL half of `services/broker/tca.py`. One row per closed trade, written
once: a fill's cost does not change after the fact, and re-measuring it from
a tick feed that has since rolled off would replace a real number with an
empty one.

`measured` is stored as a column rather than inferred from NULLs, because
"we looked and the ticks were gone" and "we have not looked yet" are
different states and the difference decides whether it is worth looking
again.
"""
from __future__ import annotations

import time

from backend.src.db.database import db


def record_fill_cost(cost, mt5_ticket=None, strategy: str = "",
                     sl_dist: float = 0.0) -> None:
    with db() as conn:
        conn.execute(
            "INSERT INTO execution_quality ("
            " trade_id, mt5_ticket, measured_at, open_time, direction,"
            " strategy, bucket, requested_price, fill_price, slippage_pts,"
            " spread_open_pts, spread_close_pts, spread_cost_pts, cost_pts,"
            " sl_dist, cost_r, fill_delay_s, measured"
            ") VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
            "ON CONFLICT(trade_id) DO UPDATE SET"
            " measured_at=excluded.measured_at,"
            " slippage_pts=excluded.slippage_pts,"
            " spread_open_pts=excluded.spread_open_pts,"
            " spread_close_pts=excluded.spread_close_pts,"
            " spread_cost_pts=excluded.spread_cost_pts,"
            " cost_pts=excluded.cost_pts, cost_r=excluded.cost_r,"
            " measured=excluded.measured",
            (cost.trade_id, mt5_ticket, time.time(), cost.open_time,
             cost.direction, strategy, cost.bucket, cost.requested_price,
             cost.fill_price, cost.slippage_pts, cost.spread_open_pts,
             cost.spread_close_pts, cost.spread_cost_pts, cost.cost_pts,
             sl_dist, cost.cost_r, cost.fill_delay_s,
             1 if cost.measured else 0),
        )


def costed_trade_ids() -> set:
    with db() as conn:
        return {r[0] for r in conn.execute(
            "SELECT trade_id FROM execution_quality")}


def fetch_costs(limit: int = 5000) -> list[dict]:
    with db() as conn:
        rows = conn.execute(
            "SELECT * FROM execution_quality ORDER BY open_time DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]


def mean_cost_r(strategy: str = "") -> tuple:
    """`(mean_cost_r, n)` over measured fills, optionally for one strategy.

    `(None, 0)` when nothing has been measured. Not 0.0: a strategy with no
    cost measurement is not a strategy that trades for free, and the
    meta-labeller's own label depends on telling those apart.
    """
    sql = ("SELECT AVG(cost_r), COUNT(*) FROM execution_quality "
           "WHERE measured=1 AND cost_r IS NOT NULL")
    params: tuple = ()
    if strategy:
        sql += " AND strategy=?"
        params = (strategy,)
    with db() as conn:
        row = conn.execute(sql, params).fetchone()
    if not row or not row[1]:
        return None, 0
    return float(row[0]), int(row[1])


def trades_awaiting_cost_measurement(limit: int = 500,
                                     strategy: str = "") -> list[dict]:
    """Closed trades with a real ticket and no cost row yet, oldest first."""
    sql = ("SELECT t.trade_id, t.mt5_ticket, t.direction, t.entry_price,"
           "       t.entry_low, t.entry_high, t.open_time, t.close_time,"
           "       t.close_price, t.lot_size, t.strategy, t.stop_loss,"
           "       t.tg_source "
           "FROM vantage_simulated_trades t "
           "LEFT JOIN execution_quality e ON e.trade_id = t.trade_id "
           "WHERE t.status='closed' AND t.mt5_ticket > 0 "
           "  AND e.trade_id IS NULL ")
    params: list = []
    if strategy:
        sql += " AND t.strategy=? "
        params.append(strategy)
    sql += " ORDER BY t.open_time ASC LIMIT ?"
    params.append(limit)
    with db() as conn:
        return [dict(r) for r in conn.execute(sql, tuple(params))]
