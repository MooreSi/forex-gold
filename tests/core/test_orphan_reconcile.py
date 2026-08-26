"""Repairing rows whose broker position is already gone.

`core_orphan_reconcile.py` was 28.8% covered -- 42 of its 59 statements never
executed -- in `services/positions`, one of the three areas the 2026-08-25
merge pushed below its coverage floor.

The module repairs a real, confirmed divergence: ticket 1704757612 was
orphaned by a recompile on 2026-08-04, closed at the broker for +$35.00, and
the app still read `status='open' remaining_lots=0.1 net_pnl=0` afterwards.

Most of what is worth pinning here is the module's *refusals*. It closes app
rows, so being wrong is expensive in both directions: too eager and it closes
live trades on a failed read; too shy and stranded rows stay wrong forever.
Each guard below is one of those refusals.

No broker involved: `bridge` is a SimpleNamespace and `close_trade_fn` is a
recorder, so nothing here can close anything real.
"""
from __future__ import annotations

import asyncio
import time
import types
import uuid

import pytest

from backend.src.db import database as db
from backend.src.services.positions import core_orphan_reconcile as orphan


def _open_row(conn, *, ticket=111, age_s=600.0, trade_id=None):
    """One open trade with the parent signal row its foreign key needs."""
    tid = trade_id or str(uuid.uuid4())
    sid = f"sig-{ticket}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (sid, "BUY", 2399.0, 2401.0, 2390.0, time.time() - age_s),
    )
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, net_pnl, strategy, tg_source) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'open',?,0,?,?)",
        (tid, sid, ticket, "BUY", 2399.0, 2401.0, 2400.0,
         0.1, 0.1, 2390.0, time.time() - age_s, "scale_out", "GD VIP"),
    )
    return tid


def _bridge(positions=None, history=None, positions_raise=False, history_raise=False):
    async def get_positions():
        if positions_raise:
            raise RuntimeError("bridge down")
        return positions if positions is not None else []

    async def get_position_history(ticket):
        if history_raise:
            raise RuntimeError("no history")
        return (history or {}).get(ticket, [])

    return types.SimpleNamespace(
        get_positions=get_positions, get_position_history=get_position_history)


def _exit_deal(price=2435.0, profit=35.0, swap=0.0, fee=0.0):
    return {"entry": 1, "price": price, "profit": profit, "swap": swap, "fee": fee}


def _closer():
    seen = []

    async def close_trade_fn(trade_id, reason):
        seen.append((trade_id, reason))
    return seen, close_trade_fn


def _row(conn, tid):
    r = conn.execute(
        "SELECT status, net_pnl, close_price, exit_reason FROM vantage_simulated_trades "
        "WHERE trade_id=?", (tid,)).fetchone()
    return db.row_to_dict(r) if r else None


# ── The refusals ──────────────────────────────────────────────────────────────

def test_an_empty_position_list_is_refused(fresh_db):
    """The comment in the module is the whole point: a genuinely flat account
    and a failed read look identical, and acting on it would close every open
    row at once.

    The rows here are given a complete exit deal on purpose. An earlier version
    of this test left the history empty, so deleting the empty-positions guard
    changed nothing -- the no-exit-deal guard caught it instead and the test
    passed against a mutation that would have closed every open trade. With a
    real exit deal present, this guard is the only thing standing between an
    empty read and a full sweep.
    """
    with fresh_db.db() as conn:
        _open_row(conn, ticket=111)
        _open_row(conn, ticket=222)
    seen, close_fn = _closer()

    n = asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[], history={111: [_exit_deal()], 222: [_exit_deal()]}),
        close_fn))

    assert n == 0
    assert seen == [], "an empty read must never close anything"


def test_a_failed_position_read_is_refused(fresh_db):
    with fresh_db.db() as conn:
        _open_row(conn)
    seen, close_fn = _closer()

    assert asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions_raise=True), close_fn)) == 0
    assert seen == []


def test_a_trade_still_open_at_the_broker_is_left_alone(fresh_db):
    with fresh_db.db() as conn:
        _open_row(conn, ticket=111)
    seen, close_fn = _closer()

    n = asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 111}]), close_fn))

    assert n == 0 and seen == []


def test_a_freshly_opened_row_is_left_alone(fresh_db):
    """A trade opened seconds ago may simply not be in /positions yet."""
    with fresh_db.db() as conn:
        _open_row(conn, ticket=111, age_s=5.0)
    seen, close_fn = _closer()

    n = asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 999}], history={111: [_exit_deal()]}), close_fn))

    assert n == 0 and seen == []


def test_absent_from_positions_but_with_no_exit_deal_is_left_alone(fresh_db):
    """Gone from /positions with nothing to prove it closed. Not confident
    enough to touch it."""
    with fresh_db.db() as conn:
        _open_row(conn, ticket=111)
    seen, close_fn = _closer()

    n = asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 999}], history={111: []}), close_fn))

    assert n == 0 and seen == []


def test_an_exit_deal_with_no_price_is_refused(fresh_db):
    with fresh_db.db() as conn:
        _open_row(conn, ticket=111)
    seen, close_fn = _closer()

    n = asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 999}], history={111: [_exit_deal(price=0.0)]}), close_fn))

    assert n == 0 and seen == []


# ── The repair ────────────────────────────────────────────────────────────────

def test_a_stranded_row_is_closed_and_given_the_brokers_own_pnl(fresh_db):
    """The 2026-08-04 case: broker closed it for +$35.00 while the app still
    read open with $0.

    The P&L written is the broker's deal total, not the app's fee model --
    otherwise a repaired row reports a different number from the account
    statement.
    """
    with fresh_db.db() as conn:
        tid = _open_row(conn, ticket=1704757612)
    seen, close_fn = _closer()

    n = asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 999}],
                history={1704757612: [_exit_deal(price=2435.0, profit=35.0)]}),
        close_fn))

    assert n == 1
    assert seen == [(tid, "reconciled_broker_closed")]
    with fresh_db.db() as conn:
        row = _row(conn, tid)
    assert row["net_pnl"] == 35.0
    assert row["close_price"] == 2435.0
    assert row["exit_reason"] == "reconciled_broker_closed"


def test_swap_and_fees_are_part_of_the_brokers_number(fresh_db):
    """Deal P&L is profit + swap + fee; dropping the last two is how a repaired
    row ends up subtly disagreeing with the statement."""
    with fresh_db.db() as conn:
        tid = _open_row(conn, ticket=222)
    seen, close_fn = _closer()

    asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 999}],
                history={222: [_exit_deal(profit=40.0, swap=-1.5, fee=-2.0)]}),
        close_fn))

    with fresh_db.db() as conn:
        assert _row(conn, tid)["net_pnl"] == pytest.approx(36.5)


def test_a_partial_close_sums_every_exit_deal(fresh_db):
    """Scale-outs leave several exit deals; the last one prices the close and
    all of them count toward the P&L."""
    with fresh_db.db() as conn:
        tid = _open_row(conn, ticket=333)
    seen, close_fn = _closer()

    asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 999}],
                history={333: [_exit_deal(price=2410.0, profit=10.0),
                               _exit_deal(price=2420.0, profit=20.0)]}),
        close_fn))

    with fresh_db.db() as conn:
        row = _row(conn, tid)
    assert row["net_pnl"] == 30.0
    assert row["close_price"] == 2420.0, "the LAST exit prices the close"


def test_a_close_that_raises_leaves_the_row_untouched(fresh_db):
    with fresh_db.db() as conn:
        tid = _open_row(conn, ticket=444)

    async def close_fn(trade_id, reason):
        raise RuntimeError("close path unavailable")

    n = asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 999}], history={444: [_exit_deal()]}), close_fn))

    assert n == 0
    with fresh_db.db() as conn:
        assert _row(conn, tid)["net_pnl"] == 0, "no P&L should be written if the close failed"


def test_nothing_to_do_when_there_are_no_open_rows(fresh_db):
    seen, close_fn = _closer()
    assert asyncio.run(orphan.reconcile_orphaned_trades(
        _bridge(positions=[{"ticket": 1}]), close_fn)) == 0
    assert seen == []
