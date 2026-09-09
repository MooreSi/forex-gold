"""A resting order must be re-checked against the trend, not just at placement.

reversal-engine/050, asked for by the owner: *"if there are pending/resting
trades before they execute re-evaluate whether they are still valid to ensure
they should still be executed based on market conditions"*.

**Why it needs no new rule.** The bias gate (reversal-engine/080) is evaluated
ONCE, when the order is placed. A limit order can then rest for the better part
of an hour and fill into a higher-timeframe bias that has since reversed — at
which point it is a counter-bias trade that the gate would have refused had it
been asked. Re-checking is applying the same rule at the moment it matters, not
inventing a second one, so this shares `governor.htf_bias_blocks` exactly as
040 and 080 do.

**The direct evidence is thin and is not what justifies this.** Signals whose
`htf_bias_at_fill` differs from `htf_bias` are 20 trades at -$12.08 each,
against -$2.84 for the 737 where it held — the right direction, four times
worse, and far too small a sample to carry a money-path rule on its own. What
carries it is the bias gate's own evidence: 201 counter-bias trades at
-$1,210.98 across the whole history.

**It cancels, it never closes.** A resting order has no position yet, so the
worst this can do is withdraw an order that has not filled. Nothing here can
touch an open trade, and the tests pin that.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.trading import resting_revalidation as rr


class _EA:
    def __init__(self, fail_on=()):
        self.cancelled: list[tuple] = []
        self._fail_on = set(fail_on)

    async def cancel_pending_order(self, trade_id, ticket, reason):
        if trade_id in self._fail_on:
            raise RuntimeError("EA said no")
        self.cancelled.append((trade_id, ticket, reason))
        return True


def _rows(*directions):
    return [{"trade_id": f"t{i}", "ea_ticket": 1000 + i, "direction": d,
             "status": "working"}
            for i, d in enumerate(directions)]


def _run(ea, rows, bias, rs, **kw):
    return asyncio.run(rr.revalidate_resting_orders(
        ea, rs, bias=bias, fetch=lambda: rows, **kw))


ON = {"htf_bias_gate_enabled": 1}


class TestItCancelsOrdersTheTrendHasTurnedAgainst:
    def test_a_resting_buy_is_cancelled_when_the_bias_turns_bearish(self):
        ea = _EA()

        n = _run(ea, _rows("BUY"), "bearish", ON)

        assert n == 1
        assert ea.cancelled[0][0] == "t0"
        assert ea.cancelled[0][1] == 1000

    def test_a_resting_sell_is_cancelled_when_the_bias_turns_bullish(self):
        ea = _EA()

        assert _run(ea, _rows("SELL"), "bullish", ON) == 1

    def test_the_reason_says_why_so_it_is_traceable(self):
        ea = _EA()

        _run(ea, _rows("BUY"), "bearish", ON)

        assert "bias" in ea.cancelled[0][2].lower()

    def test_only_the_offending_orders_go(self):
        """A sweep that cancels everything because one order is stale would be
        far worse than the problem."""
        ea = _EA()

        n = _run(ea, _rows("BUY", "SELL", "BUY"), "bearish", ON)

        assert n == 2
        assert {c[0] for c in ea.cancelled} == {"t0", "t2"}


class TestItLeavesEverythingElseAlone:
    def test_with_the_gate_off_nothing_is_cancelled(self):
        """Same toggle as the entry gate. Off means off everywhere."""
        ea = _EA()

        assert _run(ea, _rows("BUY", "SELL"), "bearish", {}) == 0
        assert ea.cancelled == []

    def test_with_the_gate_off_it_does_not_even_LOOK(self):
        """Cancelling nothing is not enough: `htf_bias_blocks` has its own
        toggle check, so the early return can be deleted and this sweep still
        cancels nothing -- while doing a database read every minute on every
        install that has the feature switched off. Proved by mutation; the
        assertion is that the fetch never happens.
        """
        looked = []
        ea = _EA()

        n = _run(ea, _rows("BUY"), "bearish", {},
                 )  # rs = {} -> gate off
        assert n == 0

        # and again, watching whether the row source is touched at all
        import asyncio as _a

        def _fetch():
            looked.append(1)
            return _rows("BUY")

        _a.run(rr.revalidate_resting_orders(ea, {}, bias="bearish", fetch=_fetch))

        assert looked == [], "the sweep read the pending orders with the gate off"

    def test_an_order_agreeing_with_the_bias_stays(self):
        ea = _EA()

        assert _run(ea, _rows("BUY"), "bullish", ON) == 0

    @pytest.mark.parametrize("bias", ["neutral", "", None, "unknown"])
    def test_an_unknown_or_neutral_bias_cancels_nothing(self, bias):
        """Fails open on not-knowing, exactly as the entry gate does. A sweep
        that withdrew every resting order because a feed hiccuped would be a
        self-inflicted outage."""
        ea = _EA()

        assert _run(ea, _rows("BUY", "SELL"), bias, ON) == 0

    def test_no_resting_orders_is_a_no_op(self):
        assert _run(_EA(), [], "bearish", ON) == 0


class TestItCannotMakeThingsWorse:
    def test_it_never_closes_anything(self):
        """A resting order has no position. The only verb available here is
        cancel, and nothing in the module may reach a close."""
        import inspect

        src = inspect.getsource(rr)
        for forbidden in ("close_trade", "record_close", "position_close"):
            assert forbidden not in src, f"{forbidden} has no business here"

    def test_one_failed_cancel_does_not_abandon_the_rest(self):
        """The EA can refuse a single order — a filled-in-the-meantime ticket
        is the obvious case. The sweep must carry on."""
        ea = _EA(fail_on={"t0"})

        n = _run(ea, _rows("BUY", "BUY"), "bearish", ON)

        assert n == 1
        assert ea.cancelled[0][0] == "t1"

    def test_a_fetch_that_throws_is_survived(self):
        """This runs on a loop. It must not take the cycle down."""
        def _boom():
            raise RuntimeError("db gone")

        assert asyncio.run(rr.revalidate_resting_orders(
            _EA(), ON, bias="bearish", fetch=_boom)) == 0

    def test_a_row_with_no_ticket_is_skipped_not_guessed(self):
        ea = _EA()
        rows = [{"trade_id": "t0", "ea_ticket": 0, "direction": "BUY"}]

        assert _run(ea, rows, "bearish", ON) == 0
        assert ea.cancelled == []
