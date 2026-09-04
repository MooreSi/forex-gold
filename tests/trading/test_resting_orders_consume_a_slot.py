"""A resting order holds a trade slot, exactly like an open position.

The owner settled this on 2026-09-04, after a cap of 3 produced more than 3:

    "whether it is a resting order or a market order the EA should manage the
     max number of allowable trades as set within the gui"

That answers the open question `reversal_engine_repo.claim_vantage_signal_
activation` had been carrying since 2026-08-30 ("whether a resting order should
consume a trade slot is a money decision for the owner ... until it is answered
this path can still over-open against the cap on its own").

Before this, a resting order consumed no slot at EITHER end:

  * not when placed -- the Reversal Engine's claim has no cap in its WHERE at
    all, and the Limit Runner path creates its signal already 'pending' with a
    'working' order beside it, which nothing counted;
  * not when it filled -- `apply_pending_fill` INSERTs an `open` row directly
    (broker/repo.py), and the only market-order backstop lives in `open_trade`,
    which that path never calls.

So N resting orders became N open trades over any cap, with nothing consulted.

The rule now: one slot is held from the moment an order exists until the
position it becomes is closed. The three ways to hold one are exclusive --
an open position, a resting order, or an open in flight -- and the last two
overlap for exactly one path, which is what the double-count tests below pin.

Nothing here places an order: these are the DB-level gates, called directly.
"""
from __future__ import annotations

import json
import time

import pytest

from backend.src.services.trading import signal_state_repo as ssr


def _set_cap(n: int):
    from backend.src.db.database import db
    with db() as conn:
        conn.execute("UPDATE vantage_risk_settings SET max_open_trades=? WHERE id=1", (n,))


def _signal(signal_id, status="pending"):
    from backend.src.db.database import db
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,"
            "entry_low,entry_high,stop_loss,lot_size,status,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Test", "BUY", 4000.0, 4002.0, 3990.0, 0.1, status, time.time()))


def _open_trade(trade_id, signal_id=None):
    from backend.src.db.database import db
    signal_id = signal_id or f"sig-{trade_id}"
    _signal(signal_id, status="active")
    with db() as conn:
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,"
            "direction,entry_low,entry_high,entry_price,lot_size,remaining_lots,"
            "stop_loss,status,open_time,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, 111, "BUY", 4000.0, 4002.0, 4000.0, 0.1, 0.1,
             3990.0, "open", time.time(), "scalp"))


def _pending_order(trade_id, signal_id=None, status="working", signal_status="pending"):
    """A resting order at the broker, with the signal row its placement path
    leaves behind."""
    from backend.src.db.database import db
    signal_id = signal_id or f"sig-{trade_id}"
    _signal(signal_id, status=signal_status)
    with db() as conn:
        conn.execute(
            """INSERT INTO vantage_pending_orders
               (trade_id,signal_id,tg_message_id,channel_name,direction,price,stop_loss,
                tps_json,pcts_json,be_at_pos,tp_open,lot_size,ea_ticket,status,created_at,
                strategy)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, None, "Reversal Engine", "BUY", 3995.0, 3985.0,
             json.dumps({"1": 4005.0}), json.dumps([1.0]), 0, 0, 0.1, 55501,
             status, time.time(), "limit_runner"),
        )


class TestTheSlotCount:
    """`count_trade_slots_used` is the one number every gate compares against
    the GUI's Max Open Trades."""

    def test_an_empty_book_uses_no_slots(self, fresh_db):
        assert ssr.count_trade_slots_used() == 0

    def test_an_open_position_uses_a_slot(self, fresh_db):
        _open_trade("t-1")

        assert ssr.count_trade_slots_used() == 1

    def test_a_resting_order_uses_a_slot(self, fresh_db):
        """The change. A working pending order is a trade the account is
        committed to; the broker will fill it with no further decision from
        anyone."""
        _pending_order("p-1")

        assert ssr.count_trade_slots_used() == 1

    def test_a_filled_order_is_counted_once_as_the_position_it_became(self, fresh_db):
        """apply_pending_fill flips 'working' to 'filled' and inserts the open
        row in ONE transaction, so the slot transfers with no gap and no
        double charge."""
        _pending_order("p-1", status="filled")
        _open_trade("p-1-pos", signal_id="sig-p-1-pos")

        assert ssr.count_trade_slots_used() == 1

    def test_a_cancelled_order_releases_its_slot(self, fresh_db):
        _pending_order("p-1", status="cancelled")

        assert ssr.count_trade_slots_used() == 0

    def test_an_in_flight_claim_uses_a_slot(self, fresh_db):
        """Unchanged behaviour, restated: this is what stops two simultaneous
        signals both passing a cap of one."""
        _signal("sig-1", status="activating")

        assert ssr.count_trade_slots_used() == 1

    def test_a_claim_holding_its_own_resting_order_is_ONE_slot(self, fresh_db):
        """The Reversal Engine's pending path claims its signal to
        'activating' and leaves it there while its order rests -- the signal
        only reaches 'active' at fill, inside apply_pending_fill. Counting the
        claim and the order separately would charge one order two slots and
        halve the cap for that path alone."""
        _pending_order("p-1", signal_status="activating")

        assert ssr.count_trade_slots_used() == 1

    def test_slots_add_up_across_all_three_kinds(self, fresh_db):
        _open_trade("t-1")
        _pending_order("p-1")
        _signal("sig-flight", status="activating")

        assert ssr.count_trade_slots_used() == 3


class TestASignalDoesNotBlockItself:
    """The claim comes FIRST and `open_trade` runs second, for the same signal.
    A backstop that counts in-flight claims without excluding the caller's own
    refuses every trade the normal path ever tries: claim sig-1 -> 1 slot used
    -> "max open trades reached (1)" on its own open. Caught by
    test_signal_resolution_characterization, which opens under a cap of 1."""

    def test_a_claim_counts_against_everyone_else(self, fresh_db):
        _signal("sig-1", status="activating")

        assert ssr.count_trade_slots_used() == 1

    def test_but_not_against_the_signal_it_belongs_to(self, fresh_db):
        _signal("sig-1", status="activating")

        assert ssr.count_trade_slots_used(exclude_signal_id="sig-1") == 0

    def test_its_own_resting_order_is_excluded_too(self, fresh_db):
        """A pending order that fills reaches open_trade under its own signal
        id on some paths; its resting row is the same slot, not a second one."""
        _pending_order("p-1", signal_id="sig-1")

        assert ssr.count_trade_slots_used(exclude_signal_id="sig-1") == 0

    def test_an_open_row_on_the_same_signal_still_counts(self, fresh_db):
        """Only the not-yet-open half is excluded. A position that exists is a
        slot however it got there, and dropping it would let one signal hold
        two."""
        _open_trade("t-1", signal_id="sig-1")

        assert ssr.count_trade_slots_used(exclude_signal_id="sig-1") == 1

    def test_other_signals_are_untouched_by_the_exclusion(self, fresh_db):
        _signal("sig-1", status="activating")
        _signal("sig-2", status="activating")
        _pending_order("p-9")

        assert ssr.count_trade_slots_used(exclude_signal_id="sig-1") == 2


class TestTheClaimIsStillGranted:
    """Controls. Without these the file would pass against a claim that
    refuses everything, which would stop all trading."""

    def test_a_free_slot_is_claimable(self, fresh_db):
        _set_cap(3)
        _open_trade("t-1")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 1

    def test_resting_orders_below_the_cap_do_not_block(self, fresh_db):
        _set_cap(3)
        _pending_order("p-1")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 1


class TestTheClaimCountsRestingOrders:

    def test_the_cap_filled_by_resting_orders_alone_refuses_a_claim(self, fresh_db):
        _set_cap(3)
        _pending_order("p-1")
        _pending_order("p-2")
        _pending_order("p-3")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 0

    def test_a_claim_refused_on_the_cap_leaves_the_signal_alone(self, fresh_db):
        """A refused claim must not consume the signal -- the scheduler retries
        it once a slot frees."""
        from backend.src.db.database import db
        _set_cap(1)
        _pending_order("p-1")
        _signal("sig-new")

        ssr.claim_signal_activation("sig-new")

        with db() as conn:
            status = conn.execute("SELECT status FROM vantage_signals WHERE signal_id=?",
                                  ("sig-new",)).fetchone()[0]
        assert status == "pending"

    def test_a_mixed_book_at_the_cap_refuses(self, fresh_db):
        """Two open positions and one resting order is three trades, however
        they are spelled."""
        _set_cap(3)
        _open_trade("t-1")
        _open_trade("t-2")
        _pending_order("p-1")
        _signal("sig-new")

        assert ssr.claim_signal_activation("sig-new") == 0

    def test_the_refusal_explains_the_resting_orders(self, fresh_db):
        """The reason reaches the user through skip_reason strings, and "max
        reached" against a visibly empty Active Trades tab is the confusing
        case this whole change creates."""
        _set_cap(2)
        _pending_order("p-1")
        _pending_order("p-2")
        _signal("sig-new")

        reason = ssr.explain_failed_claim("sig-new")

        assert "2 resting" in reason


class TestTheMarketOrderBackstop:
    """`open_trade`'s own check -- the gate for the paths that never claim a
    signal at all (manual market orders, IME). It raises before any bridge
    call, so the fake below needs no methods: an order reaching a broker is
    what the empty call log rules out."""

    class _NoOrderBridge:
        """No place_order at all. If the gate ever let a call through, this
        fails loudly instead of quietly placing something."""
        place_order_calls: list = []

    def test_a_market_order_is_refused_when_resting_orders_fill_the_cap(self, fresh_db):
        import asyncio
        from backend.src.services.trading import open_trade as ot
        _set_cap(2)
        _pending_order("p-1")
        _pending_order("p-2")
        _signal("sig-new")

        with pytest.raises(ValueError, match="Max open trades"):
            asyncio.run(ot.open_trade(
                self._NoOrderBridge(), signal_id="sig-new", direction="BUY",
                entry_low=4000.0, entry_high=4002.0, stop_loss=3990.0,
                tp1=4010.0, lot_size=0.1, strategy="scale_out"))

    def test_the_refusal_says_where_the_slots_went(self, fresh_db):
        import asyncio
        from backend.src.services.trading import open_trade as ot
        _set_cap(1)
        _pending_order("p-1")
        _signal("sig-new")

        with pytest.raises(ValueError, match="1 resting"):
            asyncio.run(ot.open_trade(
                self._NoOrderBridge(), signal_id="sig-new", direction="BUY",
                entry_low=4000.0, entry_high=4002.0, stop_loss=3990.0,
                tp1=4010.0, lot_size=0.1, strategy="scale_out"))


class TestTheReversalEnginesOwnClaim:
    """Its pending path had no cap in its WHERE clause at all."""

    def test_it_refuses_once_the_cap_is_reached(self, fresh_db):
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        _set_cap(2)
        _open_trade("t-1")
        _pending_order("p-1")
        _signal("sig-new")

        assert re_db.claim_vantage_signal_activation("sig-new") == 0

    def test_it_still_claims_when_a_slot_is_free(self, fresh_db):
        """Control -- this engine placing nothing at all is a worse failure
        than it placing one too many."""
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        _set_cap(2)
        _open_trade("t-1")
        _signal("sig-new")

        assert re_db.claim_vantage_signal_activation("sig-new") == 1

    def test_a_granted_claim_still_stamps_activated_at(self, fresh_db):
        """release_stranded_activations releases any 'activating' row whose
        activated_at is NULL, on the reasoning that a claim with no recorded
        time cannot be one a live process is running. The cap must not cost
        the stamp."""
        from backend.src.db.database import db
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        _set_cap(2)
        _signal("sig-new")

        re_db.claim_vantage_signal_activation("sig-new")

        with db() as conn:
            stamped = conn.execute(
                "SELECT activated_at FROM vantage_signals WHERE signal_id=?",
                ("sig-new",)).fetchone()[0]
        assert stamped is not None
