"""docs/todo/bugs/049: profit sync corrected `net_pnl` and left `gross_pnl` and
`realised_pnl` holding the app's close-time estimate.

Before every close and partial close, `realised_pnl` and `net_pnl` move by the
same amount (close_trade.record_close, trade_repo.apply_full_close,
apply_partial_close*). Profit sync was the one place that split them: it
replaced `net_pnl` with the broker's figure and nothing else. Three rows of
2026-09-04 (bugs/025, closed against a price of 0) kept a `gross_pnl` of about
-$44,800 against real losses of $264-$636; trade 058ee59e kept a
`realised_pnl` of +3.11 against a broker loss of -50.90.

The fix adds the same correction to all three columns in the same statement,
and a boot backfill repairs rows that were synced before it.

`trade_repo.apply_profit_sync` is not on the frozen close-path list; the
runtime's `_schedule_profit_sync` is a one-line delegate and is unchanged.

No broker: `bridge` is a stub returning canned deal history. Nothing here can
place, close or modify an order.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from backend.migrations import backfills
from backend.src.services.trading import profit_sync


def _bridge(deals):
    async def get_positions():
        return []

    async def get_position_history(ticket):
        return deals.get(int(ticket), [])

    async def get_deal_history(days):
        return [d for ds in deals.values() for d in ds]

    return types.SimpleNamespace(
        get_positions=get_positions,
        get_position_history=get_position_history,
        get_deal_history=get_deal_history,
    )


def _deals(profit):
    return [
        {"entry": 0, "price": 2400.0, "volume": 0.1, "profit": 0.0, "swap": 0.0, "fee": 0.0},
        {"entry": 1, "price": 2410.0, "volume": 0.1, "profit": profit, "swap": 0.0, "fee": 0.0},
    ]


def _seed(conn, *, trade_id, ticket, gross, realised, net, mt5_profit=None):
    sid = f"sig-{trade_id}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", (sid, "BUY", 2399.0, 2401.0, 2390.0))
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
        " entry_price, lot_size, remaining_lots, stop_loss, status, open_time, "
        " gross_pnl, realised_pnl, net_pnl, mt5_profit) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'closed',0,?,?,?,?)",
        (trade_id, sid, ticket, "BUY", 2399.0, 2401.0, 2400.0, 0.1, 0.0,
         2390.0, gross, realised, net, mt5_profit))


def _cols(db, trade_id):
    with db.db() as conn:
        return dict(conn.execute(
            "SELECT gross_pnl, realised_pnl, net_pnl, mt5_profit "
            "FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone())


def test_a_zero_price_close_has_all_three_columns_corrected(fresh_db):
    """The bugs/025 shape: closed against 0, estimated at -44,821, broker -264."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111,
              gross=-44821.0, realised=-44821.0, net=-44821.0)

    asyncio.run(profit_sync.sync_profit(tid, 111, _bridge({111: _deals(-264.0)})))

    row = _cols(fresh_db, tid)
    assert row["net_pnl"] == pytest.approx(-264.0)
    assert row["realised_pnl"] == pytest.approx(-264.0)
    assert row["gross_pnl"] == pytest.approx(-264.0)


def test_an_estimate_with_the_wrong_sign_is_corrected(fresh_db):
    """Trade 058ee59e: the app said +3.11, the broker said -50.90."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=222, gross=3.11, realised=3.11, net=3.11)

    asyncio.run(profit_sync.sync_profit(tid, 222, _bridge({222: _deals(-50.90)})))

    row = _cols(fresh_db, tid)
    assert row["realised_pnl"] == pytest.approx(-50.90)
    assert row["gross_pnl"] == pytest.approx(-50.90)


def test_gross_after_a_partial_close_moves_by_the_correction_only(fresh_db):
    """After a partial close, gross_pnl holds only the final leg (record_close
    writes the remaining lots' P&L there) while realised and net hold the
    total. Overwriting gross with the broker's total would change what the
    column means; the correction is added instead."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        # partial 20 + final 10 = 30 estimated; broker says 33
        _seed(conn, trade_id=tid, ticket=333, gross=10.0, realised=30.0, net=30.0)

    asyncio.run(profit_sync.sync_profit(tid, 333, _bridge({333: _deals(33.0)})))

    row = _cols(fresh_db, tid)
    assert row["net_pnl"] == pytest.approx(33.0)
    assert row["realised_pnl"] == pytest.approx(33.0)
    assert row["gross_pnl"] == pytest.approx(13.0)


def test_a_second_sync_changes_nothing(fresh_db):
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=444, gross=3.11, realised=3.11, net=3.11)
    bridge = _bridge({444: _deals(-50.90)})

    asyncio.run(profit_sync.sync_profit(tid, 444, bridge))
    first = _cols(fresh_db, tid)
    asyncio.run(profit_sync.sync_profit(tid, 444, bridge))

    assert _cols(fresh_db, tid) == first


# ── The repair for rows synced before the fix ────────────────────────────────

def test_the_backfill_repairs_a_row_synced_before_the_fix(fresh_db):
    """Synced by the old code: net and mt5_profit are the broker's, gross and
    realised are still the estimate."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=555, gross=-44821.0, realised=-44821.0,
              net=-264.0, mt5_profit=-264.0)
        backfills.run(conn)

    row = _cols(fresh_db, tid)
    assert row["realised_pnl"] == pytest.approx(-264.0)
    assert row["gross_pnl"] == pytest.approx(-264.0)
    assert row["net_pnl"] == pytest.approx(-264.0), "net_pnl is never touched"


def test_the_backfill_leaves_unsynced_and_agreeing_rows_alone(fresh_db):
    """Not yet synced (mt5_profit NULL) means net is still an estimate too,
    so there is nothing to repair from. A row that already agrees is not
    rewritten, which is what makes running it on every boot safe."""
    unsynced, agreeing = uuid.uuid4().hex[:16], uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=unsynced, ticket=666, gross=5.0, realised=5.0, net=7.0)
        _seed(conn, trade_id=agreeing, ticket=777, gross=12.0, realised=40.0,
              net=40.0, mt5_profit=40.0)
        backfills.run(conn)
        backfills.run(conn)

    assert _cols(fresh_db, unsynced)["realised_pnl"] == pytest.approx(5.0)
    assert _cols(fresh_db, unsynced)["gross_pnl"] == pytest.approx(5.0)
    assert _cols(fresh_db, agreeing)["gross_pnl"] == pytest.approx(12.0)


def test_the_backfill_is_idempotent_on_a_repaired_row(fresh_db):
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=888, gross=10.0, realised=30.0,
              net=33.0, mt5_profit=33.0)
        backfills.run(conn)
        backfills.run(conn)

    row = _cols(fresh_db, tid)
    assert row["gross_pnl"] == pytest.approx(13.0)
    assert row["realised_pnl"] == pytest.approx(33.0)
