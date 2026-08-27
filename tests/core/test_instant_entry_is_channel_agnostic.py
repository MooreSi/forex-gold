"""Immediate Market Entry recognises the same triggers on every channel.

The scan pipeline used to pick the IME trigger parser from the channel's
configured `parser_format`:

    _ime_gate = is_gd2_message(text) if parser_fmt == 'gd2' else "XAU" in text.upper()
    _instant  = parse_gd2_instant_entry(text) if parser_fmt == 'gd2' else parse_instant_entry(text)

`parse_instant_entry` requires a literal "XAU... BUY NOW". So on a
format_ab channel -- Gold Diggers VIP -- a market entry worded "Buy Gold
Now", "Buy Zone Now" or "XAU USD BUY" (no NOW) matched nothing and was
never executed at all, while the byte-identical message on a gd2 channel
fired. Reported live 2026-08-27: "it didn't execute the gold diggers vip
market buys at all".

No order is placed here: `_process_instant_entry_impl` is replaced with a
recorder, so the assertion is "the trigger reached the market-entry path",
never "a trade was opened".
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.signals import scan_messages as sm


class _FakeTgReader:
    def __init__(self, messages):
        self._messages = messages

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return {}

    def get_group_name(self, group_id):
        return "TestChannel"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _run_scan(text, parser_fmt):
    """Returns the (direction, price) tuple the instant-entry path was
    handed, or None if the message never reached it."""
    db.save_channel_parser_config("TestChannel", parser_fmt, "", True, True, "test")
    db.update_risk_settings({"immediate_market_entry": 1, "accept_tg_signals": 1})

    e = TradingRuntime.__new__(TradingRuntime)
    e._tg_reader = _FakeTgReader(
        [{"id": "m1", "group_id": "g1", "text": text, "timestamp": _now_iso()}])
    e._cfg = {}
    e._bridge = mock.Mock()
    e._dpm_candles = None
    e._tg_off_warn_state = {}

    fired = []

    async def _fake_instant(msg, tg_id, group_id, channel_name, txt, direction, price, *a, **k):
        fired.append((direction, price))

    with mock.patch.object(db, "should_generate_signals_here", return_value=True), \
         mock.patch.object(sm, "_process_instant_entry_impl", _fake_instant):
        asyncio.run(e._scan_messages())
    return fired[0] if fired else None


EVERY_FORMAT = pytest.mark.parametrize("parser_fmt", ["format_ab", "gd2", "auto"])


@EVERY_FORMAT
@pytest.mark.parametrize("text,direction", [
    ("XAUUSD Buy Now", "BUY"),        # Format A/B's own wording
    ("XAU Sell Now", "SELL"),
    ("Buy Gold Now", "BUY"),          # GD2's "Gold" noun
    ("Sell Zone Now", "SELL"),        # GD2's "Zone" noun
    ("XAU USD BUY", "BUY"),           # GD2, "NOW" omitted
])
def test_every_market_entry_wording_fires_on_every_channel(
    fresh_db, parser_fmt, text, direction,
):
    assert _run_scan(text, parser_fmt) == (direction, None)


@EVERY_FORMAT
def test_an_explicit_price_still_comes_through(fresh_db, parser_fmt):
    assert _run_scan("XAUUSD Buy Now 4293", parser_fmt) == ("BUY", 4293.0)


@EVERY_FORMAT
def test_a_message_carrying_its_own_levels_is_not_an_instant_entry(fresh_db, parser_fmt):
    """It is a full signal and must go through the normal parsers, or the
    trade opens at market with no stop."""
    text = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\n\nSL 4527"
    assert _run_scan(text, parser_fmt) is None


@EVERY_FORMAT
def test_ordinary_chat_never_fires_a_market_entry(fresh_db, parser_fmt):
    assert _run_scan("who is buying gold here then", parser_fmt) is None
