"""A withdrawal must survive the EA reporting the order gone.

`docs/todo/bugs/051`. The owner chose on 2026-09-10 that a resting order
failing its re-check is **withdrawn and re-armed**, not cancelled — he reversed
the permanent-cancel default that same day. In production it was cancelled
every time, one second later, by the EA:

1. revalidation cancels the order at the broker (that is how you withdraw one)
   and marks the row `withdrawn`;
2. the EA's `CheckPendingOrders` sees an order it tracked with no matching
   position — indistinguishable from an expiry or a manual cancel;
3. it reports `pending_order_cancelled`;
4. `_on_pending_order_cancelled` applied it unconditionally: row and signal
   both to `cancelled`, out of the sweep's `('working','withdrawn')` set, and
   the setup was gone.

Ten setups withdrawn, ten signals cancelled, zero re-arms, zero trades, between
2026-09-09 and 2026-09-11. The fingerprint was a row reading `cancelled` with
`withdraw_count = 1`.

Every test that existed stopped before step 3, which is why nothing caught it.
These do not: they deliver the echo.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.services.broker import repo as broker_repo


def _pending(trade_id="t1", signal_id="s1", status="working"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Telegram Auto (chan)", "BUY", 4148.0, 4148.0, 4141.0,
             0.10, "pending", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_pending_orders (trade_id,signal_id,tg_message_id,"
            "channel_name,direction,price,stop_loss,tps_json,pcts_json,be_at_pos,"
            "tp_open,lot_size,ea_ticket,status,created_at,strategy) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, "tg1", "chan", "BUY", 4148.0, 4141.0,
             json.dumps({1: 4151.0}), json.dumps([0.25]), 0, 1, 0.10, 999,
             status, time.time(), "limit_runner"),
        )


def _statuses(trade_id="t1", signal_id="s1"):
    with db.db() as conn:
        po = conn.execute(
            "SELECT status, withdraw_count FROM vantage_pending_orders WHERE trade_id=?",
            (trade_id,)).fetchone()
        sig = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone()
    return po[0], po[1], sig[0]


def _echo(trade_id="t1", reason="expired"):
    bridge = ea_bridge.EABridge(engine=None)
    asyncio.run(bridge._on_pending_order_cancelled(
        {"trade_id": trade_id, "reason": reason}))


class TestOurOwnWithdrawalEchoingBack:
    def test_a_withdrawn_row_is_left_withdrawn(self, fresh_db):
        _pending()
        broker_repo.mark_pending_order_withdrawn("t1", "news blackout", time.time())

        _echo()

        status, withdraw_count, _ = _statuses()
        assert status == "withdrawn"
        assert withdraw_count == 1

    def test_the_signal_behind_it_is_left_alone(self, fresh_db):
        """The half that made it unrecoverable. `apply_pending_cancelled`
        cancels the signal too, and the sweep re-places from that signal —
        so cancelling it is what removed the last way back."""
        _pending()
        broker_repo.mark_pending_order_withdrawn("t1", "news blackout", time.time())

        _echo()

        assert _statuses()[2] == "pending"

    def test_it_stays_in_the_set_the_sweep_reads(self, fresh_db):
        """`fetch_revalidatable_pending_orders` loads `('working','withdrawn')`. Anything
        else is invisible to the re-arm, whatever the row says."""
        _pending()
        broker_repo.mark_pending_order_withdrawn("t1", "news blackout", time.time())

        _echo()

        assert "t1" in {r["trade_id"] for r in broker_repo.fetch_revalidatable_pending_orders()}

    def test_repeated_echoes_do_not_wear_it_down(self, fresh_db):
        """The EA re-reports on every poll while the order is off the book."""
        _pending()
        broker_repo.mark_pending_order_withdrawn("t1", "news blackout", time.time())

        for _ in range(5):
            _echo()

        status, withdraw_count, sig = _statuses()
        assert (status, withdraw_count, sig) == ("withdrawn", 1, "pending")


class TestARealCancellationStillCancels:
    """The negative control, and the reason this is a status check rather than
    a blanket ignore: an order that really did expire, or that the owner pulled
    in the terminal, must still be cancelled and must still cancel its signal."""

    def test_a_working_order_reported_gone_is_cancelled(self, fresh_db):
        _pending()

        _echo()

        status, _, sig = _statuses()
        assert status == "cancelled"
        assert sig == "cancelled"

    def test_a_working_order_leaves_the_sweeps_set(self, fresh_db):
        _pending()

        _echo()

        assert "t1" not in {r["trade_id"] for r in broker_repo.fetch_revalidatable_pending_orders()}


class TestTheWholeWayRound:
    def test_withdraw_echo_then_rearm_puts_the_order_back(self, fresh_db):
        """The sequence the owner asked for, end to end: the gate turns against
        the order, it comes off the book, the EA says so, and when the gate
        clears the sweep re-places it under a new ticket."""
        _pending()
        broker_repo.mark_pending_order_withdrawn("t1", "news blackout", time.time())
        _echo()

        row = next(r for r in broker_repo.fetch_revalidatable_pending_orders()
                   if r["trade_id"] == "t1")
        broker_repo.mark_pending_order_rearmed("t1", 1234, time.time())

        status, withdraw_count, sig = _statuses()
        assert row["status"] == "withdrawn"
        assert status == "working"
        assert sig == "pending"
        with db.db() as conn:
            ticket = conn.execute(
                "SELECT ea_ticket FROM vantage_pending_orders WHERE trade_id='t1'"
            ).fetchone()[0]
        assert ticket == 1234
