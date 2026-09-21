"""An IME fill reports itself like any other execution, not as a summary.

What the owner received on a template-managed IME fill:

    Immediate Signal Entry  (Gold Diggers VIP)
    BUY at $0.00  |  lot 0.10  |  ticket pending
    SL: $4377.27 (SL from template "30 TP1 SL50 and Trail")

Two separate faults in three lines:

* the numbers were the ones in hand at order time. An EA Template row is
  INSERTed as a placeholder (mt5_ticket=0, entry_price=0.0) and only gains
  its real ticket and fill price when the first leg fills, so the alert
  printed the placeholder -- "$0.00", "pending" -- as if that were the fill.
  Every other opening path already waits this out (runtime's
  `_await_trade_promotion`); IME did not, because IME never sent the
  standard message at all.
* the shape carried no TP ladder, no spread, no channel, no node and no
  "Executed via" -- everything `fmt_trade_open` reports. Nothing about an
  execution report is IME-specific; only how the entry was TRIGGERED is.

So the fix is not a better summary: it is the standard message, with one
added line naming IME, sent once the row can answer for itself.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from backend.src.services.telegram import alerts as ta
from backend.src.services.trading import instant_entry


def _promoted_row():
    return {
        "trade_id": "abc123", "direction": "BUY", "mt5_ticket": 2046843929,
        "entry_price": 4376.76, "entry_low": 4374.0, "entry_high": 4377.0,
        "lot_size": 0.1, "stop_loss": 4371.76,
        "tp1": 4380.76, "tp2": 4381.76, "tp3": 4383.76,
        "strategy": "tpl:30 TP1 SL50 and Trail", "tg_source": "Reversal Engine",
        "managed_by": "ea",
    }


class _Tick:
    spread_points = 22.0


class TestTheFormatter:
    def test_it_is_the_standard_execution_message(self, fresh_db):
        """Same body as fmt_trade_open -- not a second, diverging layout."""
        row = _promoted_row()
        standard = ta.fmt_trade_open(row, _Tick(), {})
        ime = ta.fmt_instant_entry(row, _Tick(), "")

        for line in standard.splitlines():
            assert line in ime, f"the IME message dropped {line!r}"

    def test_it_carries_the_real_ticket_entry_and_ladder(self, fresh_db):
        msg = ta.fmt_instant_entry(_promoted_row(), _Tick(), "")

        assert "MT5 Ticket: 2046843929" in msg
        assert "Entry: 4376.76  (range 4374.0–4377.0)" in msg
        assert "TP1: 4380.76" in msg
        assert "TP3: 4383.76" in msg
        assert "Spread: 22 pts" in msg
        # "Engine:", not "Channel:", since 2026-09-21 -- the Reversal Engine
        # is this app's own, and calling it a channel made its executions
        # indistinguishable from copied Telegram signals. What this test is
        # about is that the line is CARRIED, not what it is called.
        assert "Engine: Reversal Engine" in msg
        assert "Executed via: EA" in msg
        assert "$0.00" not in msg

    def test_a_market_entry_shows_no_degenerate_range(self, fresh_db):
        """IME is a market fill, so entry_low == entry_high == the fill and
        "(range 4376.76-4376.76)" is noise. A real entry zone still prints
        (test_it_carries_the_real_ticket_entry_and_ladder covers that)."""
        row = dict(_promoted_row(), entry_low=4376.76, entry_high=4376.76)
        msg = ta.fmt_instant_entry(row, _Tick(), "")

        assert "Entry: 4376.76" in msg
        assert "range" not in msg

    def test_it_says_the_entry_was_immediate(self, fresh_db):
        msg = ta.fmt_instant_entry(_promoted_row(), _Tick(), "")
        assert "Immediate Signal Entry" in msg

    def test_it_keeps_the_stop_note(self, fresh_db):
        """The one thing the old summary said that fmt_trade_open does not:
        where the stop came from. bugs/023 wording, unchanged."""
        note = '_(SL from template "30 TP1 SL50 and Trail")_'
        msg = ta.fmt_instant_entry(_promoted_row(), _Tick(), note)
        assert note in msg


class TestItWaitsOutThePlaceholder:
    def test_it_sends_the_promoted_row_not_the_placeholder(self, fresh_db):
        """The row is read again after the legs fill, so the alert carries
        the broker's ticket and fill price rather than 0 / 0.0."""
        placeholder = dict(_promoted_row(), mt5_ticket=0, entry_price=0.0)
        rows = [placeholder, placeholder, _promoted_row()]
        sent = []

        async def _fake_send(text, trade_id=None, event_type="", **kw):
            sent.append((text, trade_id, event_type))
            return True

        with mock.patch.object(instant_entry.trade_repo, "get_trade",
                               side_effect=lambda _tid: rows.pop(0)), \
             mock.patch.object(instant_entry.telegram_alerts, "send_message",
                               side_effect=_fake_send):
            asyncio.run(instant_entry._send_instant_entry_alert(
                "abc123", _Tick(), "", "fallback", poll=0.0))

        assert len(sent) == 1, "the IME fill must be reported exactly once"
        text, trade_id, event_type = sent[0]
        assert trade_id == "abc123"
        assert event_type == "instant_entry"
        assert "MT5 Ticket: 2046843929" in text
        assert "Entry: 4376.76" in text

    def test_it_gives_up_and_still_reports_an_unfilled_grid(self, fresh_db):
        """A grid whose legs all sit unfilled is legitimate -- the alert must
        not be withheld for ever waiting on a promotion that never comes."""
        placeholder = dict(_promoted_row(), mt5_ticket=0, entry_price=0.0)
        sent = []

        async def _fake_send(text, trade_id=None, event_type="", **kw):
            sent.append(text)
            return True

        with mock.patch.object(instant_entry.trade_repo, "get_trade",
                               return_value=placeholder), \
             mock.patch.object(instant_entry.telegram_alerts, "send_message",
                               side_effect=_fake_send):
            asyncio.run(instant_entry._send_instant_entry_alert(
                "abc123", _Tick(), "", "fallback", timeout=0.0, poll=0.0))

        assert len(sent) == 1
        assert "MT5 Ticket: pending" in sent[0]

    def test_the_fallback_is_used_when_the_row_has_gone(self, fresh_db):
        """No row, no standard message -- but the fill still happened, so
        the summary is sent rather than nothing."""
        sent = []

        async def _fake_send(text, trade_id=None, event_type="", **kw):
            sent.append(text)
            return True

        with mock.patch.object(instant_entry.trade_repo, "get_trade",
                               return_value=None), \
             mock.patch.object(instant_entry.telegram_alerts, "send_message",
                               side_effect=_fake_send):
            asyncio.run(instant_entry._send_instant_entry_alert(
                "abc123", _Tick(), "", "fallback summary"))

        assert sent == ["fallback summary"]


class TestWhenItIsSent:
    """Waiting is the exception, not the rule.

    Every Python-managed IME trade already has its ticket and fill price the
    moment open_trade returns, so its message goes out immediately -- the
    promotion wait exists only for an EA Template placeholder, and making
    every fill wait for it would delay the one alert whose whole point is
    that the entry was immediate.
    """

    def _send_spy(self, sent):
        async def _fake_send(text, trade_id=None, event_type="", **kw):
            sent.append(text)
            return True
        return _fake_send

    def test_a_promoted_row_is_reported_at_once(self, fresh_db):
        sent = []

        async def _go():
            with mock.patch.object(instant_entry.trade_repo, "get_trade",
                                   return_value=_promoted_row()), \
                 mock.patch.object(instant_entry.telegram_alerts, "send_message",
                                   side_effect=self._send_spy(sent)):
                await instant_entry._report_instant_entry(
                    "abc123", _Tick(), "", "fallback")
                await asyncio.sleep(0)

        asyncio.run(_go())
        assert len(sent) == 1
        assert "MT5 Ticket: 2046843929" in sent[0]

    def test_a_placeholder_row_is_not_reported_yet(self, fresh_db):
        sent = []

        async def _go():
            with mock.patch.object(
                    instant_entry.trade_repo, "get_trade",
                    return_value=dict(_promoted_row(), mt5_ticket=0,
                                      entry_price=0.0)), \
                 mock.patch.object(instant_entry.telegram_alerts, "send_message",
                                   side_effect=self._send_spy(sent)):
                await instant_entry._report_instant_entry(
                    "abc123", _Tick(), "", "fallback")
                await asyncio.sleep(0)
                assert sent == [], (
                    "a template placeholder was announced as $0.00 / pending "
                    "instead of waiting for the leg to fill"
                )
                for t in asyncio.all_tasks() - {asyncio.current_task()}:
                    t.cancel()

        asyncio.run(_go())

    def test_a_forwarded_trade_keeps_the_summary(self, fresh_db):
        """Executed on the VPS: the row is in the VPS's DB, so there is
        nothing here to read back and nothing to wait for."""
        sent = []

        async def _go():
            with mock.patch.object(instant_entry.trade_repo, "get_trade",
                                   return_value=None), \
                 mock.patch.object(instant_entry.telegram_alerts, "send_message",
                                   side_effect=self._send_spy(sent)):
                await instant_entry._report_instant_entry(
                    "abc123", _Tick(), "", "fallback summary")
                await asyncio.sleep(0)

        asyncio.run(_go())
        assert sent == ["fallback summary"]
