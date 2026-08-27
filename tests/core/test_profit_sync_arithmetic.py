"""What profit sync writes to the account and the trade row.

`profit_sync.sync_profit` reconciles the app's own estimate against the
broker's real deal P&L and corrects the simulation account balance by the
difference. It carries the most consequential SQL left outside the data layer,
and it is about to move into `trade_repo`, so what it does is pinned first.

The arithmetic has two idempotency properties that the comments spell out and
that a careless move would break:

- `mt5_profit` stays NULL until every leg has settled, so the correction on
  each retry is the INCREMENT a newly-closed leg added -- never the same money
  applied twice.
- `initial_risk` is written as an absolute, not an increment, so re-running it
  as later legs settle converges rather than accumulating.

Both are asserted below by running the sync twice.

No broker: `bridge` is a stub returning canned deal history. Nothing here can
place or close anything.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from backend.src.services.trading import profit_sync


def _bridge(deals, positions=()):
    async def get_positions():
        return [{"ticket": t} for t in positions]

    async def get_position_history(ticket):
        return deals.get(int(ticket), [])

    async def get_deal_history(days):
        return [d for ds in deals.values() for d in ds]

    return types.SimpleNamespace(
        get_positions=get_positions,
        get_position_history=get_position_history,
        get_deal_history=get_deal_history,
    )


def _open_deal(price, volume=0.1):
    return {"entry": 0, "price": price, "volume": volume, "profit": 0.0,
            "swap": 0.0, "fee": 0.0}


def _close_deal(profit, swap=0.0, fee=0.0):
    return {"entry": 1, "price": 2410.0, "volume": 0.1,
            "profit": profit, "swap": swap, "fee": fee}


def _seed(conn, *, trade_id, ticket, net_pnl, initial_sl=None, balance=1000.0):
    sid = f"sig-{trade_id}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", (sid, "BUY", 2399.0, 2401.0, 2390.0))
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
        " entry_price, lot_size, remaining_lots, stop_loss, initial_sl, status, "
        " open_time, net_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,'closed',0,?)",
        (trade_id, sid, ticket, "BUY", 2399.0, 2401.0, 2400.0, 0.1, 0.1,
         2390.0, initial_sl, net_pnl))
    conn.execute("UPDATE vantage_simulation_account SET balance=? WHERE id=1", (balance,))


def _row(db, trade_id):
    with db.db() as conn:
        r = conn.execute(
            "SELECT net_pnl, mt5_profit, initial_risk FROM vantage_simulated_trades "
            "WHERE trade_id=?", (trade_id,)).fetchone()
        bal = conn.execute(
            "SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0]
    return dict(r), float(bal)


def test_the_balance_is_corrected_by_the_difference_from_our_estimate(fresh_db):
    """We estimated 30; the broker says 35. The account gains the 5 we missed,
    not the whole 35."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=30.0, balance=1000.0)

    bridge = _bridge({111: [_open_deal(2400.0), _close_deal(35.0)]})
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))

    row, balance = _row(fresh_db, tid)
    assert row["net_pnl"] == pytest.approx(35.0)
    assert balance == pytest.approx(1005.0), "corrected by 5, not by 35"


def test_running_it_again_does_not_apply_the_same_money_twice(fresh_db):
    """The property the comments lean on. Once net_pnl matches the broker,
    the correction is zero and nothing is written."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=30.0, balance=1000.0)

    bridge = _bridge({111: [_open_deal(2400.0), _close_deal(35.0)]})
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))
    _, after_first = _row(fresh_db, tid)
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))
    _, after_second = _row(fresh_db, tid)

    assert after_second == pytest.approx(after_first)


def test_a_sub_penny_difference_is_left_alone(fresh_db):
    """Below a cent, correcting is noise -- and it would rewrite net_pnl for
    nothing.

    Note the ORDER: the broker total is rounded to 2dp BEFORE the one-cent
    threshold is applied, so 35.005 rounds to 35.01 and does correct. I assumed
    the opposite and the test caught me. 35.004 is the case this guards.
    """
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=35.0, balance=1000.0)

    bridge = _bridge({111: [_open_deal(2400.0), _close_deal(35.004)]})
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))

    _, balance = _row(fresh_db, tid)
    assert balance == pytest.approx(1000.0)


def test_a_half_penny_rounds_up_and_does_correct(fresh_db):
    """The other side of that boundary, pinned so the rounding order cannot
    change unnoticed."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=35.0, balance=1000.0)

    bridge = _bridge({111: [_open_deal(2400.0), _close_deal(35.005)]})
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))

    _, balance = _row(fresh_db, tid)
    assert balance == pytest.approx(1000.01)


def test_swap_and_fees_are_part_of_the_brokers_number(fresh_db):
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=0.0, balance=1000.0)

    bridge = _bridge({111: [_open_deal(2400.0), _close_deal(40.0, swap=-1.5, fee=-2.0)]})
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))

    row, _ = _row(fresh_db, tid)
    assert row["net_pnl"] == pytest.approx(36.5)


def test_mt5_profit_is_stamped_once_everything_has_settled(fresh_db):
    """It is the "done" marker -- schedule_profit_sync stops retrying on it."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=0.0)

    bridge = _bridge({111: [_open_deal(2400.0), _close_deal(35.0)]})
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))

    row, _ = _row(fresh_db, tid)
    assert row["mt5_profit"] == pytest.approx(35.0)


def test_initial_risk_is_absolute_so_re_running_converges(fresh_db):
    """Written as a value, not added to -- otherwise every retry inflates the
    R:R the row reports."""
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=0.0, initial_sl=2390.0)

    bridge = _bridge({111: [_open_deal(2400.0), _close_deal(35.0)]})
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))
    first, _ = _row(fresh_db, tid)
    asyncio.run(profit_sync.sync_profit(tid, 111, bridge))
    second, _ = _row(fresh_db, tid)

    assert first["initial_risk"] is not None
    assert second["initial_risk"] == pytest.approx(first["initial_risk"])


def test_nothing_is_written_when_no_leg_has_closed(fresh_db):
    tid = uuid.uuid4().hex[:16]
    with fresh_db.db() as conn:
        _seed(conn, trade_id=tid, ticket=111, net_pnl=30.0, balance=1000.0)

    bridge = _bridge({111: [_open_deal(2400.0)]})     # opened, never closed
    out = asyncio.run(profit_sync.sync_profit(tid, 111, bridge))

    row, balance = _row(fresh_db, tid)
    assert out is None
    assert balance == pytest.approx(1000.0)
    assert row["mt5_profit"] is None
