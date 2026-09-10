"""Say so on Telegram when a resting order is withdrawn, and when it comes back.

docs/todo/limit-orders/050. Written RED, before the alert.

`revalidate_resting_orders` cancels a live broker order and says so only to the
log. From the owner's side an order simply stops existing: no message, and the
next thing seen is the setup not filling when price reaches the zone. Every
other consequential event on this system announces itself -- fills, TP hits, SL
moves, closes.

Owner, 2026-09-10: *"You should send a telegram message advising if a limit
order is discarded on this basis."*

**Damping** (owner, 2026-09-10, QUESTIONS.md #3). Withdraw-and-re-arm can flap:
a news window opening and closing, momentum flipping across two candles. Each
flap is literally a discard, and announcing every one would be a dozen messages
for a single setup on a bad morning. So: the FIRST withdrawal and the FIRST
re-placement are announced, then the signal goes quiet, and the total is
reported once in the message when the order finally fills.

**The count is persisted, not held in a module global.** A global would not
survive a restart, and a restart mid-flap would restart the noise -- while also
losing the number the final message is supposed to carry. It rides on the
order's own row, which is where 040 already keeps its status.

**One counter answers both questions**, which is why there is no second column:
a withdrawal is the first when the count is 0, and a re-placement is the first
when the count is 1.

The messages are asserted on their rendered text, not on "a send was
attempted". bugs/038 was a refusal naming the wrong rule and bugs/036 an IME
note promising a follow-up that could not arrive; both are this failure mode,
and both would pass a test that only counted calls.

No broker is reachable: the EA is a fake that records cancellations and
placements, and `send_message` is patched to collect text.
"""
from __future__ import annotations

import asyncio
import json
import time
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.trading import resting_revalidation as rr
from backend.src.services.trading import trade_repo

RESTING_PRICE = 4415.00
STOP_LOSS = 4403.00
TPS = {1: 4428.0, 2: 4435.0, 3: 4445.0}
TICKET = 5551
LOT = 0.10
CHANNEL = "GOLD DIGGERS INSTITUTIONAL"
STRATEGY = "template:GD Instituational - single"
NEAR_PRICE = RESTING_PRICE + 9.0


class _FakeEA:
    def __init__(self):
        self.cancelled: list[tuple] = []
        self.placed: list[dict] = []

    async def cancel_pending_order(self, trade_id, ticket, reason):
        self.cancelled.append((trade_id, ticket, reason))
        return True

    async def place_pending_order(self, trade_id, direction, price, lot_size, stop_loss,
                                  tps, pcts, be_at_pos, strategy, expire_minutes=240.0,
                                  close_full_on_last=True, trail_mode=None, template=None):
        self.placed.append({"trade_id": trade_id, "price": price})
        return {"type": "pending_order_placed", "ticket": TICKET + len(self.placed)}


class _Tick:
    def __init__(self, px):
        self.bid = px - 0.25
        self.ask = px + 0.25
        self.mid = px


def _seed(status="working", age_secs=300.0):
    now = time.time() - age_secs
    trade_repo.insert_pending_order_signal(
        "sig-aaaa", f"Telegram Auto ({CHANNEL})", "BUY",
        4410.0, RESTING_PRICE, STOP_LOSS, TPS, LOT,
        "Limit order pending", now, None,
        ("trade-aaaa", "sig-aaaa", "tg1", CHANNEL, "BUY", RESTING_PRICE, STOP_LOSS,
         json.dumps({str(k): v for k, v in TPS.items()}), json.dumps([0.25, 0.25, 0.25]),
         0, 1, LOT, TICKET, status, now, STRATEGY),
    )


def _order_row():
    with db.db() as conn:
        return db.row_to_dict(conn.execute(
            "SELECT * FROM vantage_pending_orders WHERE trade_id=?",
            ("trade-aaaa",)).fetchone())


def _rs():
    return {"htf_bias_gate_enabled": 1, "resting_revalidation_enabled": 1,
            "risk_per_trade_pct": 0.5}


def _sweep(ea, sent, bias="bullish", px=NEAR_PRICE, send_raises=False):
    async def _capture(text, trade_id=None, event_type="", reply_markup=None):
        if send_raises:
            raise RuntimeError("telegram unreachable")
        sent.append({"text": text, "trade_id": trade_id, "event_type": event_type})
        return True

    with mock.patch("backend.src.services.telegram.alerts.send_message",
                    side_effect=_capture):
        return asyncio.run(rr.revalidate_resting_orders(
            ea, _rs(), bias,
            tick=_Tick(px),
            dpm_candles=[{"open": 4420.0, "close": 4428.0,
                          "high": 4429.0, "low": 4419.0}],
        ))


class TestTheWithdrawalMessage:
    def test_a_withdrawal_is_announced(self, fresh_db):
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish")

        assert ea.cancelled, "nothing was withdrawn, so this proves nothing"
        assert len(sent) == 1, f"expected one message, got {sent}"

    def test_it_names_the_order_and_the_gate_that_refused_it(self, fresh_db):
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish")

        text = sent[0]["text"]
        assert "BUY" in text
        assert "4415" in text, "the message does not say which order went"
        assert "GOLD DIGGERS" in text, "the message does not say which channel"
        assert "bias" in text.lower(), (
            f"the message does not say why it went: {text}"
        )

    def test_the_reason_is_the_gate_own_words_not_a_generic_line(self, fresh_db):
        """bugs/038 was a refusal message naming the wrong rule. The reason
        string is already threaded through cancel_pending_order; there is
        nothing to invent."""
        _seed()
        ea, sent = _FakeEA(), []

        with mock.patch("backend.src.utils.news_calendar.check_news_blackout",
                        return_value=(False, "NFP in 4 minutes")):
            _sweep(ea, sent)

        assert "NFP in 4 minutes" in sent[0]["text"]


class TestTheReArmMessage:
    def test_a_re_placement_is_announced_too(self, fresh_db):
        """A withdrawal must never be left looking permanent when the order is
        back on the book."""
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish")     # withdrawn  -> message 1
        _sweep(ea, sent, bias="bullish")     # re-armed   -> message 2

        assert ea.placed, "nothing was re-placed, so this proves nothing"
        assert len(sent) == 2, f"expected two messages, got {sent}"
        assert "4415" in sent[1]["text"]

    def test_it_says_how_long_the_order_has_left(self, fresh_db):
        """The re-placed order runs on the ORIGINAL clock, so "it's back" is
        only half the story -- it may be back with four minutes to live."""
        _seed(age_secs=45 * 60)
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish")
        _sweep(ea, sent, bias="bullish")

        assert "15" in sent[1]["text"], (
            f"the re-arm message does not say how long is left: {sent[1]['text']}"
        )


class TestTheDamping:
    def test_a_second_withdrawal_is_silent(self, fresh_db):
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish")     # message 1
        _sweep(ea, sent, bias="bullish")     # message 2
        _sweep(ea, sent, bias="bearish")     # quiet

        assert len(ea.cancelled) == 2, "the second withdrawal did not happen"
        assert len(sent) == 2, f"the flap was announced again: {sent}"

    def test_and_so_is_a_second_re_placement(self, fresh_db):
        _seed()
        ea, sent = _FakeEA(), []

        for bias in ("bearish", "bullish", "bearish", "bullish"):
            _sweep(ea, sent, bias=bias)

        assert len(ea.placed) == 2, "the second re-placement did not happen"
        assert len(sent) == 2

    def test_the_count_survives_a_restart(self, fresh_db):
        """A module global would forget this and restart the noise -- and lose
        the number the final message is supposed to carry."""
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish")

        assert _order_row()["withdraw_count"] == 1, (
            "the flap count is not on the order's row, so nothing outlives "
            "this process"
        )


class TestTheFinalMessage:
    """The other half of the damping decision: flaps go quiet, but the total is
    reported once, in the message when the order finally fills. Without this,
    suppressing the noise would also suppress the fact that it happened."""

    def _fill(self, sent):
        from backend.src.services.broker import ea_bridge
        bridge = ea_bridge.EABridge(engine=None)

        async def _capture(text, *a, **k):
            sent.append(text)

        with mock.patch("backend.src.services.telegram.alerts.send_message",
                        side_effect=_capture):
            asyncio.run(bridge._on_pending_order_filled({
                "trade_id": "trade-aaaa", "ticket": 9001, "fill_price": RESTING_PRICE,
            }))
            asyncio.run(asyncio.sleep(0))

    def test_a_fill_reports_how_often_the_order_flapped(self, fresh_db):
        _seed()
        ea, sent = _FakeEA(), []
        for bias in ("bearish", "bullish", "bearish", "bullish"):
            _sweep(ea, sent, bias=bias)
        assert _order_row()["withdraw_count"] == 2, "the flap did not happen"

        fill_msgs: list[str] = []
        self._fill(fill_msgs)

        assert fill_msgs, "no fill message at all"
        assert "2 times" in fill_msgs[0], (
            f"the fill message hides that the order flapped: {fill_msgs[0]}"
        )

    def test_an_order_that_rested_undisturbed_says_nothing_extra(self, fresh_db):
        """The note has to earn its place in a message read on every fill."""
        _seed()

        fill_msgs: list[str] = []
        self._fill(fill_msgs)

        assert fill_msgs
        assert "withdrawn" not in fill_msgs[0].lower(), (
            f"a clean fill carried a flap note: {fill_msgs[0]}"
        )

    def test_nor_does_one_withdrawn_and_replaced_exactly_once(self, fresh_db):
        """One withdrawal and one re-placement were BOTH already announced as
        they happened. Repeating the count would be a third message about the
        same two events."""
        _seed()
        ea, sent = _FakeEA(), []
        _sweep(ea, sent, bias="bearish")
        _sweep(ea, sent, bias="bullish")

        fill_msgs: list[str] = []
        self._fill(fill_msgs)

        assert fill_msgs
        assert "times" not in fill_msgs[0]


class TestTheControls:
    def test_a_sweep_that_withdraws_nothing_says_nothing(self, fresh_db):
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bullish")

        assert ea.cancelled == []
        assert sent == []

    def test_a_send_failure_does_not_take_the_sweep_down(self, fresh_db):
        """The sweep runs on a loop and its docstring promises it never raises.
        An alert is not worth breaking that for -- and the order must still be
        recorded as withdrawn, or it can never be re-armed."""
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish", send_raises=True)

        assert len(ea.cancelled) == 1
        assert _order_row()["status"] == "withdrawn"

    def test_the_order_is_still_withdrawn_when_the_alert_is_silenced(self, fresh_db):
        """Damping must never reach the ORDER. Only the message is suppressed."""
        _seed()
        ea, sent = _FakeEA(), []

        _sweep(ea, sent, bias="bearish")
        _sweep(ea, sent, bias="bullish")
        _sweep(ea, sent, bias="bearish")

        assert _order_row()["status"] == "withdrawn"
        assert _order_row()["withdraw_count"] == 2
