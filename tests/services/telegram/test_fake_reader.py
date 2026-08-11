"""FakeTelegramReader feeds the REAL signal pipeline (stage2 phase5/020).

The fake replays scripted Telegram-shaped messages through the same
buffer contract the Telethon reader fills, so scan_messages -> parser ->
signal row runs unmodified. A fake that bypassed the parser would prove
nothing.

No test here can reach a broker or Telegram: the runtime is built with an
unconfigured HTTP bridge (empty url — every call returns None/[]/error
dicts without network), and the fake reader has no client at all.
"""
from __future__ import annotations

import asyncio
import inspect

from backend.src.services.telegram.fake_reader import FakeTelegramReader
from backend.src.services.telegram.reader import TelegramReader

# The reader surface the runtime, scan pipeline and status panels consume.
CONSUMED = [
    "startup", "shutdown", "get_buffer_messages", "wait_for_new_message",
    "get_active_group_slots", "get_group_name", "get_status",
]
CONSUMED_PROPS = ["auth_state", "auth_error", "telethon_available"]


def _surface_gaps(fake_cls) -> list[str]:
    gaps = []
    for name in CONSUMED:
        real = getattr(TelegramReader, name, None)
        fake = getattr(fake_cls, name, None)
        if fake is None:
            gaps.append(f"missing: {name}")
            continue
        if list(inspect.signature(real).parameters) != list(inspect.signature(fake).parameters):
            gaps.append(f"signature drift: {name}")
        if inspect.iscoroutinefunction(real) != inspect.iscoroutinefunction(fake):
            gaps.append(f"async mismatch: {name}")
    for name in CONSUMED_PROPS:
        if not isinstance(getattr(fake_cls, name, None), property):
            gaps.append(f"missing property: {name}")
    return gaps


def test_fake_reader_matches_reader_surface():
    assert _surface_gaps(FakeTelegramReader) == []


def test_surface_check_can_fail():
    """Negative control: a fake missing a consumed method is caught."""

    class _Impostor:
        pass

    gaps = _surface_gaps(_Impostor)
    assert "missing: get_buffer_messages" in gaps


def test_scripted_messages_buffer_in_order_with_ids():
    reader = FakeTelegramReader({}, scenario={"signals": [
        {"at": 0, "channel": "Debug Channel", "text": "first"},
        {"at": 0, "channel": "Debug Channel", "text": "second"},
    ]})
    reader.feed_due(now=1.0)  # both due immediately
    msgs = reader.get_buffer_messages(limit=10)
    assert [m["text"] for m in msgs] == ["second", "first"]  # newest first
    assert all(m["id"] for m in msgs)
    assert len({m["id"] for m in msgs}) == 2
    slots = reader.get_active_group_slots()
    assert len(slots) == 1
    gid = next(iter(slots))
    assert reader.get_group_name(gid) == "Debug Channel"


def test_messages_release_on_schedule_not_before():
    reader = FakeTelegramReader({}, scenario={"signals": [
        {"at": 5, "channel": "Debug Channel", "text": "later"},
    ]})
    reader.feed_due(now=1.0)
    assert reader.get_buffer_messages() == []
    reader.feed_due(now=6.0)
    assert [m["text"] for m in reader.get_buffer_messages()] == ["later"]


def test_scripted_message_reaches_parser(fresh_db):
    """The killer test: a scripted gold-format signal, fed through the fake
    buffer, comes out of the REAL scan_messages pipeline as a signal row in
    the database."""
    from backend.src.runtime import TradingRuntime

    config = {
        "starting_balance": 1000.0, "anthropic_api_key": "",
        "mt5_bridge_url": "", "mt5_native_bridge_enabled": False,
        "telegram_api_id": "", "telegram_api_hash": "",
        "sessions_dir": "./data/test_sessions",
    }
    engine = TradingRuntime(config)
    reader = FakeTelegramReader(config, scenario={"signals": [{
        "at": 0, "channel": "Debug Channel",
        "text": ("This is not financial advice. Use appropriate risk management "
                 "if you're going to trade.\n"
                 "Buy Gold 2400 - 2402\n"
                 "Stop Loss 2392\n"
                 "TP1 2410  TP2 2418  TP3 2426"),
    }]})
    engine.set_telegram_reader(reader)
    reader.feed_due(now=1.0)

    new_signals = asyncio.run(engine._scan_messages())

    assert len(new_signals) == 1
    parsed = new_signals[0]
    assert parsed["direction"] == "BUY"
    assert parsed["entry_low"] == 2400.0 and parsed["entry_high"] == 2402.0

    # The persisted TG-signal row (vantage_signals fills only on execution).
    with fresh_db.db() as conn:
        row = conn.execute(
            "SELECT direction, entry_low, entry_high, stop_loss, tp1, group_name "
            "FROM vantage_tg_signals"
        ).fetchone()
    assert row is not None
    assert row["direction"] == "BUY"
    assert row["entry_low"] == 2400.0 and row["entry_high"] == 2402.0
    assert row["stop_loss"] == 2392.0
    assert row["tp1"] == 2410.0
    assert row["group_name"] == "Debug Channel"
