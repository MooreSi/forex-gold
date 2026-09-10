"""A resting order is judged against the market it is about to enter.

docs/todo/limit-orders/040. Written RED, before the fix.

A limit order is accepted by the broker long before it fills, and MT5 fills it
directly — there is no round trip back to Python at the moment of the fill. So
every entry gate that could not be evaluated at placement time is never
evaluated at all, unless something asks again while the order rests.

`revalidate_resting_orders` is that something. Today it asks exactly one
question, the higher-timeframe bias, and only when the trend gate is switched
on. Meanwhile a *queued* Telegram signal — the same setup, waiting in Python
instead of at the broker — is re-checked against the trading schedule, the news
blackout, the fill delay, the pre-trade filters and the last M5 candle
(`pending_activation.py:432-525`). reversal-engine/100 built that and said so in
its own "Still open":

    Only the bias is re-checked on a resting order. Schedule and news are not.

Owner, 2026-09-10: full parity with the queued path.

**The gates must be the SAME functions, not copies.** A second implementation
of "are we in a blackout" is how two routes come to disagree — the shape behind
bugs/024, reversal-engine/080 and bugs/034. These tests patch each gate at the
module where it is DEFINED, so an implementation that copies one, or that binds
it with `from x import f` at import time, fails here. `resting_revalidation.py`
already calls `_gov.htf_bias_blocks` through its module for exactly this
reason.

**Proximity** (owner, 2026-09-10): the cheap bias check keeps running on every
sweep; the full gate set runs only once price is within 10 points of the
resting price. Far enough ahead of the fill to act, close enough that a
condition which would have cleared by fill time does not cancel an order an
hour early.

**This sweep cancels. It never closes.** The module's own docstring promises
that and says a test asserts it by name. That test is here and must survive the
widening.

No broker is reachable: the EA is a fake that records cancellations.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from unittest.mock import patch

import pytest

from backend.src.services.trading import resting_revalidation as rr

# The 2026-09-10 order: a BUY resting at the top of its zone.
RESTING_PRICE = 4415.00
TICKET = 5551

# Owner decision, 2026-09-10 (QUESTIONS #3 in the pack is the alert half; this
# is the proximity half). Named here so a change to the constant is a change to
# this file too.
NEAR = RESTING_PRICE + 9.0    # 9 points away — inside the window
FAR = RESTING_PRICE + 11.0    # 11 points away — outside it


class _FakeEA:
    def __init__(self):
        self.cancelled: list[tuple] = []

    async def cancel_pending_order(self, trade_id, ticket, reason):
        self.cancelled.append((trade_id, ticket, reason))
        return True


class _Tick:
    def __init__(self, px):
        self.bid = px - 0.25
        self.ask = px + 0.25
        self.mid = px


def _row(**over):
    """One working resting order, shaped like a real `vantage_pending_orders`
    row — every column, not only the ones an assertion reads."""
    row = {
        "trade_id": "trade-aaaa", "signal_id": "sig-aaaa", "tg_message_id": "tg1",
        "channel_name": "GOLD DIGGERS INSTITUTIONAL", "direction": "BUY",
        "price": RESTING_PRICE, "stop_loss": 4403.0,
        # NOT the live signal's own 4418/4422/4427. Against its 4403 stop that
        # is 3 points of reward for 12 of risk -- 0.25:1, genuinely below the
        # 0.75:1 minimum -- so a row shaped that way is refused by the R:R
        # filter for real, and "nothing is withdrawn when every gate passes"
        # would be a control over a row that does not pass them.
        "tps_json": '{"1": 4428.0, "2": 4435.0, "3": 4445.0}',
        "pcts_json": "[0.25, 0.25, 0.25]", "be_at_pos": 0, "tp_open": 1,
        "lot_size": 0.10, "ea_ticket": TICKET, "status": "working",
        "created_at": time.time() - 300.0, "resolved_at": None,
        "strategy": "template:GD Instituational - single",
    }
    row.update(over)
    return row


def _rs(**over):
    rs = {
        "htf_bias_gate_enabled": 1,
        "resting_revalidation_enabled": 1,
        "risk_per_trade_pct": 0.5,
    }
    rs.update(over)
    return rs


# `revalidate_resting_orders` does not take a tick or candles yet — it asks one
# question that needs neither. Passing only what the current signature accepts
# keeps today's run informative: the tests for behaviour that genuinely does not
# exist fail, and the controls for behaviour that already holds still pass,
# instead of every test in the file failing with the same TypeError. Once the
# signature grows, this shim passes everything and stops doing anything at all
# — `test_the_signature_takes_the_market_it_is_judging_against` below is what
# stops it hiding a missing parameter.
_ACCEPTS = set(inspect.signature(rr.revalidate_resting_orders).parameters)


def _sweep(ea, rs=None, bias="bullish", rows=None, px=NEAR, candles=None):
    kwargs = {"fetch": lambda: rows if rows is not None else [_row()]}
    if "tick" in _ACCEPTS:
        kwargs["tick"] = _Tick(px)
    if "dpm_candles" in _ACCEPTS:
        kwargs["dpm_candles"] = candles
    return asyncio.run(rr.revalidate_resting_orders(
        ea, rs if rs is not None else _rs(), bias, **kwargs))


def _bullish_candle():
    return [{"open": 4420.0, "close": 4428.0, "high": 4429.0, "low": 4419.0}]


def _bearish_candle():
    return [{"open": 4428.0, "close": 4420.0, "high": 4429.0, "low": 4419.0}]


def test_the_signature_takes_the_market_it_is_judging_against(fresh_db):
    """Schedule, news, R:R and momentum cannot be asked without a price and a
    candle. Asserted directly so the shim above can never quietly stand in for
    a parameter that was never added."""
    assert {"tick", "dpm_candles"} <= _ACCEPTS, (
        f"revalidate_resting_orders takes {sorted(_ACCEPTS)} — it cannot judge "
        f"a resting order against the current market"
    )


class TestTheGatesItDoesNotAskYet:
    """Each of these refuses a queued signal today and a resting order never."""

    def test_the_trading_schedule_withdraws_the_order(self, fresh_db):
        ea = _FakeEA()
        with patch("backend.src.services.risk.schedule.check_trading_schedule",
                   return_value=(False, "outside the trading window")):
            _sweep(ea, candles=_bullish_candle())

        assert len(ea.cancelled) == 1, (
            "a resting order survived the trading schedule closing on it"
        )
        assert "trading window" in ea.cancelled[0][2]

    def test_a_news_blackout_withdraws_the_order(self, fresh_db):
        ea = _FakeEA()
        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, candles=_bullish_candle())

        assert len(ea.cancelled) == 1, (
            "a resting order survived a news blackout opening on it"
        )
        assert "NFP" in ea.cancelled[0][2]

    def test_the_fill_delay_withdraws_the_order(self, fresh_db):
        ea = _FakeEA()
        with patch("backend.src.services.risk.governor.fill_too_soon",
                   return_value="filled 40s after the signal"):
            _sweep(ea, candles=_bullish_candle())

        assert len(ea.cancelled) == 1
        assert "40s" in ea.cancelled[0][2]

    def test_the_pre_trade_filters_withdraw_the_order(self, fresh_db):
        ea = _FakeEA()
        with patch("backend.src.services.risk.governor.check_pre_trade_filters",
                   return_value="R:R 0.4:1 below the 1.5:1 minimum"):
            _sweep(ea, candles=_bullish_candle())

        assert len(ea.cancelled) == 1
        assert "0.4:1" in ea.cancelled[0][2]

    def test_a_contrary_m5_candle_withdraws_the_order(self, fresh_db):
        """The momentum check the queued path applies: a candle against the
        trade suggests the move into the zone is a fakeout."""
        ea = _FakeEA()
        _sweep(ea, candles=_bearish_candle())

        assert len(ea.cancelled) == 1, (
            "a BUY rested through a bearish M5 candle that would have deferred "
            "the same signal on the queued path"
        )


class TestTheBiasGateItAlreadyAsks:
    def test_a_turned_bias_still_withdraws(self, fresh_db):
        ea = _FakeEA()
        _sweep(ea, bias="bearish", candles=_bullish_candle())

        assert len(ea.cancelled) == 1
        assert "bias" in ea.cancelled[0][2].lower()

    def test_the_bias_is_asked_even_far_from_the_price(self, fresh_db):
        """Proximity gates the EXPENSIVE checks. The bias is one comparison
        against a value the caller already holds, and an order resting an hour
        away from a reversed trend should not wait to be withdrawn."""
        ea = _FakeEA()
        _sweep(ea, bias="bearish", px=FAR, candles=_bullish_candle())

        assert len(ea.cancelled) == 1

    def test_an_unreadable_bias_withdraws_nothing(self, fresh_db):
        """Fail-open, unchanged. A sweep that pulled the whole book because a
        price feed hiccuped would be a self-inflicted outage."""
        ea = _FakeEA()
        _sweep(ea, bias="", candles=_bullish_candle())

        assert ea.cancelled == []


class TestProximity:
    def test_a_refusing_gate_is_ignored_while_price_is_far_away(self, fresh_db):
        ea = _FakeEA()
        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, px=FAR, candles=_bullish_candle())

        assert ea.cancelled == [], (
            "an order 11 points away was withdrawn over a condition that may "
            "well have cleared before price ever reached it"
        )

    def test_and_acted_on_once_price_is_near(self, fresh_db):
        ea = _FakeEA()
        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, px=NEAR, candles=_bullish_candle())

        assert len(ea.cancelled) == 1

    def test_a_sell_measures_proximity_the_other_way(self, fresh_db):
        """A SELL rests ABOVE the market and price approaches from below.
        Without this, `abs()` versus a signed comparison is untestable."""
        ea = _FakeEA()
        row = _row(direction="SELL", price=4454.0, stop_loss=4461.0)
        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, rows=[row], px=4454.0 - 9.0, candles=_bearish_candle())

        assert len(ea.cancelled) == 1


class TestTheControls:
    """Without these, "cancel everything" passes every test above."""

    def test_nothing_is_withdrawn_when_every_gate_passes(self, fresh_db):
        ea = _FakeEA()
        _sweep(ea, candles=_bullish_candle())

        assert ea.cancelled == []

    def test_the_toggle_switches_the_widened_checks_off(self, fresh_db):
        """Owner decision 2026-09-10: its own setting, so turning the trend gate
        off does not silently turn the news re-check off too."""
        ea = _FakeEA()
        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, rs=_rs(resting_revalidation_enabled=0),
                   candles=_bullish_candle())

        assert ea.cancelled == []

    def test_the_trend_gate_toggle_no_longer_governs_the_other_checks(self, fresh_db):
        """The coupling this decision exists to break: `htf_bias_gate_enabled`
        off must still leave the news re-check running."""
        ea = _FakeEA()
        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, rs=_rs(htf_bias_gate_enabled=0), candles=_bullish_candle())

        assert len(ea.cancelled) == 1

    def test_an_order_with_no_broker_ticket_is_left_alone(self, fresh_db):
        """There is nothing to withdraw, and guessing a ticket would cancel
        somebody else's order. Unchanged from today."""
        ea = _FakeEA()
        _sweep(ea, bias="bearish", rows=[_row(ea_ticket=0)],
               candles=_bullish_candle())

        assert ea.cancelled == []

    def test_a_gate_that_throws_does_not_take_the_sweep_down(self, fresh_db):
        """This runs on a loop. A bad sweep must not end the cycle — the
        module's docstring promises it never raises."""
        ea = _FakeEA()
        with patch("backend.src.utils.news_calendar.check_news_blackout",
                   side_effect=RuntimeError("calendar unreachable")):
            _sweep(ea, candles=_bullish_candle())

        assert ea.cancelled == []


class TestItNeverCloses:
    """`resting_revalidation.py`'s docstring: "It cancels; it never closes ...
    Nothing here may reach a close, and a test asserts that by name." That
    promise must survive the widening — the gate set grows, the blast radius
    does not."""

    FORBIDDEN = ("close_trade", "record_close", "_make_close_trade_ctx",
                 "partial_close_trade")

    def test_the_module_names_no_close_path(self):
        source = inspect.getsource(rr)
        # Strip the docstring, which quotes these names on purpose.
        body = source.split('"""', 2)[-1]
        for name in self.FORBIDDEN:
            assert name not in body, (
                f"the resting-order sweep can reach {name}; it may only cancel"
            )

    def test_that_check_can_fail(self):
        """Negative control: the search above must be capable of finding one."""
        assert "close_trade" in 'x = close_trade(1)'
