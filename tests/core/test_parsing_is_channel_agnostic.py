"""Every Telegram channel is parsed by the same rules.

Owner directive, 2026-08-27: "parsing rules should apply exactly the same to
all telegram channels". They did not. `classify_and_parse` branched three
ways on the channel's configured `parser_format` and only one of those
branches ('auto') ever tried both families of parser:

  * a **format_ab** channel never ran a single GD2 regex, so a
    "Buy Gold Now / <zone> / Targets / SL" message on Gold Diggers VIP was
    never parsed -- AI fallback at best, unrecognised queue at worst;
  * a **gd2** channel never ran Format A/B, so a "Direction SELL / ENTRY /
    Stop Loss / TP1.." message on that channel was never parsed either.

These pin the union. No MT5 order is placed, closed or modified anywhere in
this file -- `classify_and_parse` only reads text and writes signal rows.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.signals import scan_parse_classify as pc
from backend.src.services.telegram import alerts as telegram_alerts

# A complete signal in each family's own layout.
GD2_ZONE = ("Buy Gold Now\n4163.5 - 4158.5\nTargets\n4165.5\n4167.5\n4170\n"
            "SL/ invalid 4155.5")
GD2_XAU = ("XAU USD SELL NOW\n\n4534 - 4529\n\nTP1 4526\nTP2 4524\nTP3 4522\n\nSL 4537")
FORMAT_A = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
            "Sell Gold 4520 - 4512\nStop Loss 4524\n"
            "TP1 4510  TP2 4508  TP3 4506  TP4 4503  TP5 4500")
FORMAT_B = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
            "Direction SELL\nENTRY : 4468-4466\nStop Loss 4472\n"
            "TP1: 4464\nTP2: 4462\nTP3: 4460")
LIMITS = "BUY LIMITS GOLD @ 4073/4067 AREA\nTP 4076\nTP 4080\nTP 4085\nSL 4066"

EVERY_FORMAT = pytest.mark.parametrize("parser_fmt", ["format_ab", "gd2", "auto"])


def _parse(text, parser_fmt, *, ai_return=None, channel="TestChannel"):
    """Run one message through the pipeline. Returns (parsed, unrecognised)."""
    msg = {"id": "m1", "group_id": "g1", "text": text,
           "timestamp": "2026-08-27T10:00:00+00:00", "sender_name": "s"}
    queued: list[tuple] = []

    async def _ai(*a):
        return ai_return

    async def _send(m, *a, **k):
        return None

    with mock.patch.object(telegram_alerts, "send_message", side_effect=_send):
        parsed = asyncio.run(pc.classify_and_parse(
            "m1", "g1", channel, text, msg, parser_fmt,
            "This is not financial advice.", _ai,
            lambda *a: queued.append(a), {},
        ))
    return parsed, queued


@EVERY_FORMAT
@pytest.mark.parametrize("text,direction,low,high", [
    (GD2_ZONE, "BUY", 4158.5, 4163.5),
    (GD2_XAU, "SELL", 4529.0, 4534.0),
    (FORMAT_A, "SELL", 4512.0, 4520.0),
    (FORMAT_B, "SELL", 4466.0, 4468.0),
])
def test_a_complete_signal_parses_whatever_the_channel_is_configured_as(
    fresh_db, parser_fmt, text, direction, low, high,
):
    parsed, queued = _parse(text, parser_fmt)
    assert parsed is not None, f"{parser_fmt} channel dropped a complete signal"
    assert parsed["direction"] == direction
    assert (parsed["entry_low"], parsed["entry_high"]) == (low, high)
    assert queued == []


@EVERY_FORMAT
def test_the_limit_order_layout_is_recognised_on_every_channel(fresh_db, parser_fmt):
    """Already true before the merge -- it was checked ahead of the branching.
    Pinned here so the merge cannot lose it."""
    parsed, _ = _parse(LIMITS, parser_fmt)
    assert parsed is not None
    assert parsed["direction"] == "BUY"
    assert parsed["tp_open"] is False


@EVERY_FORMAT
def test_a_non_xauusd_signal_is_refused_on_every_channel(fresh_db, parser_fmt):
    """The currency guard lived inside the format_ab branch, so the identical
    EURUSD signal on a gd2-configured channel was never even recorded."""
    text = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
            "Direction BUY\nCurrency: EURUSD\nENTRY : 1.1180-1.1200\nStop Loss 1.1150\nTP1 1.1250")
    parsed, _ = _parse(text, parser_fmt)
    assert parsed is None
    with db.db() as conn:
        row = conn.execute(
            "SELECT status FROM vantage_tg_signals WHERE tg_message_id='m1'").fetchone()
    assert row is not None and row[0] == "unsupported_currency"


@EVERY_FORMAT
def test_a_partial_is_held_for_its_follow_up_on_every_channel(fresh_db, parser_fmt):
    """Direction + entry, levels still to come. A format_ab channel had no
    partial path at all before the merge."""
    parsed, queued = _parse("Buy Zone Now\n4163.5 - 4158.5", parser_fmt)
    assert parsed is None
    assert queued == []
    with db.db() as conn:
        row = conn.execute(
            "SELECT status FROM vantage_tg_signals WHERE tg_message_id='m1'").fetchone()
    assert row is not None and row[0] == "pending_followup"


@EVERY_FORMAT
def test_ordinary_chat_is_never_queued_as_unrecognised(fresh_db, parser_fmt):
    """The queue is for layouts nobody has taught the app yet. Widening the
    parsers must not turn it into a firehose of channel banter."""
    parsed, queued = _parse("morning everyone, charts looking heavy today", parser_fmt)
    assert parsed is None
    assert queued == []
