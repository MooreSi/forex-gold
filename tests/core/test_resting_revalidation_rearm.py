"""A withdrawn resting order comes back if the market lets it — on its
original clock, at its original price, and never twice.

docs/todo/limit-orders/040. Written RED, before the fix.

Owner, 2026-09-10: a failed re-check withdraws the broker order but does not
kill the setup. A news window or a schedule edge is temporary; the trade the
channel sent is not. The order is re-placed if every gate passes again before
its original expiry.

That is the part of 040 with teeth, and these tests exist for the three ways it
can go wrong rather than for the happy path:

1. **An immortal order.** Re-placing with a fresh `expire_minutes` restarts the
   60-minute life every time. A gate that flaps four times would keep an order
   alive for five hours, long after the setup it describes has stopped being
   true. The re-placed order expires when the original would have.
2. **A drifted order.** Re-placing at "the current near edge" instead of the
   stored price silently walks the entry. If the levels have to move for the
   order to be valid, it is not the same order — leave it withdrawn.
3. **A doubled order.** A withdrawn row that is not clearly distinguishable
   from a working one gets re-placed alongside an order that is already on the
   book. `apply_pending_cancelled` writes `status='cancelled'` and cancels the
   signal with it — final, and no basis for a re-arm — so withdrawal needs its
   own status and must leave the signal alone.

These use the real database rather than a fake repo: the whole question is what
state the row is left in, and a fake would answer it with whatever this file
imagined. `vantage_pending_orders` has no expiry column, so "the original
clock" has to be derived from `created_at` — which is exactly the sort of
detail a test written after the code would have quietly ratified.

**What is honestly green today.** The "never does X" tests — a working order is
never re-placed, an expired one is never re-placed, a failed replacement leaves
the row withdrawn — pass right now because nothing is ever re-placed at all.
They are guards for after the feature exists, not evidence about today, and
they are the ones to break deliberately once it does. The seven red tests are
the behaviour that genuinely does not exist.

No broker is reachable: the EA is a fake that records both calls.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest.mock import patch

import pytest

from backend.src.db import database as db
from backend.src.services.trading import resting_revalidation as rr
from backend.src.services.trading import trade_repo

RESTING_PRICE = 4415.00
STOP_LOSS = 4403.00
# Not the live signal's own 4418/4422/4427: against its 4403 stop that is
# 0.25:1, which the R:R filter refuses for real. A re-arm asks the same gates
# the withdrawal asked, so a row shaped that way could never come back and
# every test here would pass for the wrong reason.
TPS = {1: 4428.0, 2: 4435.0, 3: 4445.0}
TICKET = 5551
LOT = 0.10
CHANNEL = "GOLD DIGGERS INSTITUTIONAL"
STRATEGY = "template:GD Instituational - single"

# `limit_order_signal._DEFAULT_EXPIRE_MINUTES`. Imported rather than repeated
# so a change to the order's life is a change to this file's arithmetic too.
from backend.src.services.trading.limit_order_signal import _DEFAULT_EXPIRE_MINUTES

NEAR_PRICE = RESTING_PRICE + 9.0


class _FakeEA:
    def __init__(self, place_ok=True):
        self.cancelled: list[tuple] = []
        self.placed: list[dict] = []
        self._place_ok = place_ok

    async def cancel_pending_order(self, trade_id, ticket, reason):
        self.cancelled.append((trade_id, ticket, reason))
        return True

    async def place_pending_order(self, trade_id, direction, price, lot_size, stop_loss,
                                  tps, pcts, be_at_pos, strategy, expire_minutes=240.0,
                                  close_full_on_last=True, trail_mode=None, template=None):
        self.placed.append(dict(
            trade_id=trade_id, direction=direction, price=price, lot_size=lot_size,
            stop_loss=stop_loss, tps=dict(tps), strategy=strategy,
            expire_minutes=expire_minutes,
        ))
        if not self._place_ok:
            return {"type": "pending_order_open_failed", "error": "Invalid price"}
        return {"type": "pending_order_placed", "ticket": TICKET + 1}


class _Tick:
    def __init__(self, px):
        self.bid = px - 0.25
        self.ask = px + 0.25
        self.mid = px


def _seed(status="working", age_secs=300.0, trade_id="trade-aaaa",
          signal_id="sig-aaaa", ticket=TICKET):
    """One resting order in the real tables, `age_secs` old."""
    now = time.time() - age_secs
    trade_repo.insert_pending_order_signal(
        signal_id, f"Telegram Auto ({CHANNEL})", "BUY",
        4410.0, RESTING_PRICE, STOP_LOSS, TPS, LOT,
        "Limit order pending", now, None,
        (trade_id, signal_id, "tg1", CHANNEL, "BUY", RESTING_PRICE, STOP_LOSS,
         json.dumps({str(k): v for k, v in TPS.items()}), json.dumps([0.25, 0.25, 0.25]),
         0, 1, LOT, ticket, status, now, STRATEGY),
    )
    return trade_id, signal_id


def _order_row(trade_id="trade-aaaa"):
    with db.db() as conn:
        return db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_pending_orders WHERE trade_id=?", (trade_id,)).fetchone())


def _signal_row(signal_id="sig-aaaa"):
    with db.db() as conn:
        return db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_signals WHERE signal_id=?", (signal_id,)).fetchone())


def _rs(**over):
    rs = {"htf_bias_gate_enabled": 1, "resting_revalidation_enabled": 1,
          "risk_per_trade_pct": 0.5}
    rs.update(over)
    return rs


def _sweep(ea, bias="bullish", px=NEAR_PRICE, rs=None):
    """The real sweep, reading the real tables."""
    import inspect
    kwargs = {}
    accepts = set(inspect.signature(rr.revalidate_resting_orders).parameters)
    if "tick" in accepts:
        kwargs["tick"] = _Tick(px)
    if "dpm_candles" in accepts:
        kwargs["dpm_candles"] = [{"open": 4420.0, "close": 4428.0,
                                  "high": 4429.0, "low": 4419.0}]
    return asyncio.run(rr.revalidate_resting_orders(
        ea, rs if rs is not None else _rs(), bias, **kwargs))


class TestWithdrawal:
    def test_a_withdrawn_order_is_not_marked_cancelled(self, fresh_db):
        """`cancelled` is final. A row that may still come back must be
        distinguishable from one that never will, or the re-arm pass either
        misses it or resurrects genuinely dead orders."""
        _seed()
        ea = _FakeEA()

        _sweep(ea, bias="bearish")

        assert ea.cancelled, "the order was not withdrawn at all"
        assert _order_row()["status"] != "cancelled", (
            "a withdrawn order was marked cancelled — it can never be re-armed"
        )
        assert _order_row()["status"] != "working", (
            "the row still reads as working after its broker order was pulled"
        )

    def test_the_signal_is_left_alive(self, fresh_db):
        """`apply_pending_cancelled` cancels the signal along with the order.
        A withdrawal must not: the setup is what comes back."""
        _seed()
        ea = _FakeEA()

        _sweep(ea, bias="bearish")

        assert _signal_row()["status"] == "pending", (
            "withdrawing the order killed the signal behind it — reusing "
            "apply_pending_cancelled here would do exactly that"
        )


class TestTheReArm:
    def test_a_withdrawn_order_is_replaced_once_the_gates_pass(self, fresh_db):
        _seed(status="withdrawn")
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        assert len(ea.placed) == 1, "a withdrawn order was never re-placed"

    def test_it_is_replaced_at_the_original_price_and_levels(self, fresh_db):
        """Not at "the current near edge". If the levels have to move, it is a
        different trade."""
        _seed(status="withdrawn")
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        assert ea.placed[0]["price"] == pytest.approx(RESTING_PRICE)
        assert ea.placed[0]["stop_loss"] == pytest.approx(STOP_LOSS)
        assert ea.placed[0]["tps"] == pytest.approx(TPS)
        assert ea.placed[0]["lot_size"] == pytest.approx(LOT)
        assert ea.placed[0]["strategy"] == STRATEGY, (
            "the re-placed order would be managed by a different strategy"
        )

    def test_it_expires_on_the_original_clock(self, fresh_db):
        """The immortal-order failure. Seeded 45 minutes into a 60-minute life,
        so a correct re-arm asks for about 15 minutes, not another 60."""
        _seed(status="withdrawn", age_secs=45 * 60)
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        assert ea.placed[0]["expire_minutes"] == pytest.approx(15.0, abs=1.0), (
            f"re-placed with {ea.placed[0]['expire_minutes']} minutes — the "
            f"order's life restarted instead of continuing"
        )

    def test_the_row_reads_as_working_again(self, fresh_db):
        _seed(status="withdrawn")
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        row = _order_row()
        assert row["status"] == "working"
        assert row["ea_ticket"] == TICKET + 1, (
            "the row kept the dead ticket — a later cancel would target an "
            "order that no longer exists"
        )

    def test_a_failed_replacement_leaves_it_withdrawn(self, fresh_db):
        """The broker can refuse: a limit price the market has since crossed is
        "Invalid price", which is what started this whole pack. Recording it as
        working would strand a row with no order behind it."""
        _seed(status="withdrawn")
        ea = _FakeEA(place_ok=False)

        _sweep(ea, bias="bullish")

        assert _order_row()["status"] == "withdrawn"


class TestItStaysWithdrawnWhileTheMarketStillRefuses:
    """Found while implementing, not before: the first version re-placed a
    withdrawn order without asking anything, so every re-arm test above passed
    while the gates were never consulted. A condition that would pull an order
    off the book is not one to put it back under."""

    def test_a_withdrawn_order_is_not_replaced_while_the_bias_still_refuses(self, fresh_db):
        _seed(status="withdrawn")
        ea = _FakeEA()

        _sweep(ea, bias="bearish")

        assert ea.placed == [], (
            "a withdrawn BUY went back on the book into the bearish bias that "
            "withdrew it"
        )
        assert _order_row()["status"] == "withdrawn"

    def test_nor_while_a_news_blackout_is_open(self, fresh_db):
        _seed(status="withdrawn")
        ea = _FakeEA()

        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, bias="bullish")

        assert ea.placed == []

    def test_but_distance_alone_does_not_hold_it_back(self, fresh_db):
        """Proximity gates PULLING an order, not putting one back: an order
        that should be resting belongs on the book whatever the distance."""
        _seed(status="withdrawn")
        ea = _FakeEA()

        _sweep(ea, bias="bullish", px=RESTING_PRICE + 300.0)

        assert len(ea.placed) == 1


class TestTheOriginalTTL:
    def test_an_expired_withdrawn_order_is_never_replaced(self, fresh_db):
        """Past its life. The setup is gone, not waiting."""
        _seed(status="withdrawn", age_secs=(_DEFAULT_EXPIRE_MINUTES + 5) * 60)
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        assert ea.placed == [], (
            "an order past its original expiry was put back on the book"
        )

    def test_and_is_marked_expired_rather_than_left_to_retry_forever(self, fresh_db):
        _seed(status="withdrawn", age_secs=(_DEFAULT_EXPIRE_MINUTES + 5) * 60)
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        assert _order_row()["status"] not in ("withdrawn", "working"), (
            "the row will be reconsidered on every sweep for the rest of time"
        )


class TestItNeverDoubles:
    def test_a_working_order_is_never_replaced(self, fresh_db):
        """The doubling failure: one order on the book, one in the row, and a
        sweep that places a second."""
        _seed(status="working")
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        assert ea.placed == [], "a second order was placed for a live one"

    def test_a_cancelled_order_is_never_replaced(self, fresh_db):
        """`cancelled` is the user cancelling, or the broker expiring it.
        Neither comes back."""
        _seed(status="cancelled")
        ea = _FakeEA()

        _sweep(ea, bias="bullish")

        assert ea.placed == []

    def test_withdraw_then_rearm_then_withdraw_leaves_exactly_one_order(self, fresh_db):
        """The whole flap, in sequence. Counting is the point: at no moment may
        there be two live orders for one setup."""
        _seed(status="working")
        ea = _FakeEA()

        _sweep(ea, bias="bearish")      # gate turns -> withdrawn
        _sweep(ea, bias="bullish")      # gate clears -> re-placed
        _sweep(ea, bias="bearish")      # turns again -> withdrawn

        assert len(ea.cancelled) == 2, f"cancels: {ea.cancelled}"
        assert len(ea.placed) == 1, f"places: {ea.placed}"
        assert _order_row()["status"] == "withdrawn"
