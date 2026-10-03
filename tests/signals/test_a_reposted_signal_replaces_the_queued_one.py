"""A channel that re-posts a corrected signal gets one queued signal, not two.

Live 2026-10-02, Gold Diggers VIP, 18 seconds apart:

    22556  BUY  ENTRY 4180-4176  SL 4174   (09:49:10 UTC)
    22557  BUY  ENTRY 4180-4178  SL 4177   (09:49:28 UTC)

Price was above both zones, so both were queued, and the bot reported two
"Signal Detected ... Signal queued" alerts. The channel is on an EA template,
whose queued signals wait up to an hour (`_TEMPLATE_PENDING_EXPIRY_SEC`), so
both would have filled the moment price came back to 4180 -- two positions
for one setup, the first on the stop the channel had already corrected.

The owner's decision (2026-10-02): **within 120 seconds, the newer post
replaces the older.** Same channel, same direction, the older one still
queued. Outside the window both stand as separate setups.

What may be replaced is deliberately narrow, on contradiction.py's rule that
cancelling is free and closing is not:

- only `pending` signals -- one that has filled is a position, untouched;
- never one with a working EA pending order -- withdrawing that is a broker
  call, not a row update;
- only when the newer post itself became a live signal (queued or opened). A
  newer post that was skipped replaces nothing.

Nothing here reaches a broker: `open_trade` is a recording sentinel and the
bridge the shared `tests._fakes` double with a canned tick.
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.signals import repost_supersede as rs_mod
from backend.src.services.telegram import alerts as telegram_alerts
from tests._fakes import _FakeBridge

_CH = "TestChannel"
_SRC = f"Telegram Auto ({_CH})"

# The live pair, in the gd2 layout this harness's channel parses.
_FIRST = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4527"
_FIXED = "XAU USD BUY NOW\n\n4534 - 4531\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4530"
_ABOVE_ZONE = SimpleNamespace(bid=4538.0, ask=4538.5)


@pytest.fixture
def scan_db(fresh_db):
    fresh_db.update_risk_settings(
        {"accept_tg_signals": 1, "auto_execute_signals": 1, "max_open_trades": 5})
    fresh_db.save_channel_parser_config(_CH, "gd2", "", True, True, "test")
    return fresh_db


# ── end to end through scan_messages ────────────────────────────────────────

class _Reader:
    def __init__(self, msgs):
        self._msgs = msgs

    def get_buffer_messages(self, limit=100):
        return self._msgs

    def get_active_group_slots(self):
        return {}

    def get_group_name(self, group_id):
        return _CH


async def _open_sentinel(self_, **kwargs):
    _open_sentinel.calls.append(kwargs)
    return {"trade_id": "never", "entry_price": 0.0}
_open_sentinel.calls = []


def _now_iso():
    return datetime.now(timezone.utc).isoformat()


def _scan(texts):
    e = TradingRuntime.__new__(TradingRuntime)
    e._tg_reader = _Reader([
        {"id": str(22556 + i), "group_id": "g1", "text": t, "timestamp": _now_iso()}
        for i, t in enumerate(texts)])
    e._cfg = {}
    e._bridge = _FakeBridge(tick=_ABOVE_ZONE)
    e._dpm_candles = None
    e._tg_off_warn_state = {}
    _open_sentinel.calls = []
    with mock.patch.object(db, "should_generate_signals_here", return_value=True), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()), \
         mock.patch.object(TradingRuntime, "open_trade", _open_sentinel), \
         mock.patch.object(TradingRuntime, "get_open_trades", return_value=[]), \
         mock.patch.object(TradingRuntime, "_check_pre_trade_filters", return_value=None), \
         mock.patch.object(TradingRuntime, "_find_and_apply_instant_followup",
                           new=mock.AsyncMock(return_value=False)):
        asyncio.run(e._scan_messages())


def _signals():
    with db.db() as conn:
        return [db.row_to_dict(r) for r in conn.execute(
            "SELECT signal_id, status, stop_loss, entry_low FROM vantage_signals "
            "ORDER BY created_at")]


class TestTheLiveCase:
    def test_the_correction_is_the_only_signal_left_queued(self, scan_db):
        _scan([_FIRST, _FIXED])

        pending = [s for s in _signals() if s["status"] == "pending"]
        assert len(pending) == 1, _signals()
        assert pending[0]["stop_loss"] == 4530.0, "the wrong one of the pair survived"
        assert _open_sentinel.calls == []

    def test_the_first_post_is_cancelled_not_deleted(self, scan_db):
        """The record of what the channel first said is kept for the audit
        trail and the Signals table."""
        _scan([_FIRST, _FIXED])

        first = next(s for s in _signals() if s["stop_loss"] == 4527.0)
        assert first["status"] == "cancelled"

    def test_one_post_alone_is_queued_as_before(self, scan_db):
        """Control: the replacement step must not touch a lone signal."""
        _scan([_FIRST])

        assert [s["status"] for s in _signals()] == ["pending"]


# ── the rule itself, row by row ─────────────────────────────────────────────

def _signal(sid, *, direction="BUY", status="pending", age=19.0, source=_SRC):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            (sid, source, direction, 4176.0, 4180.0, 4174.0, status, time.time() - age))


def _tg(tg_id, signal_id):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_tg_signals (tg_message_id,group_id,raw_text,parsed_at,"
            "direction,signal_id,status) VALUES (?,?,?,?,?,?,?)",
            (tg_id, "g1", "x", time.time(), "BUY", signal_id, "pending"))


def _status(sid):
    with db.db() as conn:
        return conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?", (sid,)).fetchone()[0]


def _replace(tg_id="22557", direction="BUY"):
    with mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        return asyncio.run(rs_mod.supersede_earlier_pending(tg_id, _CH, direction))


@pytest.fixture
def newer(fresh_db):
    """The corrected post, already queued as its own signal."""
    _signal("new", age=0.0)
    _tg("22557", "new")
    return "new"


class TestWhatIsReplaced:
    def test_an_older_queued_signal_inside_the_window(self, newer):
        _signal("old", age=19.0)

        assert _replace() == ["old"]
        assert _status("old") == "cancelled"
        assert _status(newer) == "pending"

    def test_the_window_edge_is_120_seconds(self, newer):
        _signal("inside", age=115.0)
        _signal("outside", age=125.0)

        assert _replace() == ["inside"]
        assert _status("outside") == "pending"


class TestWhatIsNeverReplaced:
    def test_the_opposite_direction(self, newer):
        _signal("sell", direction="SELL")

        assert _replace() == []
        assert _status("sell") == "pending"

    def test_another_channels_signal(self, newer):
        _signal("other", source="Telegram Auto (GOLD DIGGERS INSTITUTIONAL)")

        assert _replace() == []
        assert _status("other") == "pending"

    def test_a_signal_that_has_already_filled(self, newer):
        """A position is behind it. Cancelling is free; closing is not."""
        _signal("filled", status="active")

        assert _replace() == []
        assert _status("filled") == "active"

    def test_a_signal_with_a_working_broker_order(self, newer):
        """Withdrawing a resting EA order is a broker call, not a row update."""
        _signal("resting")
        with db.db() as conn:
            conn.execute(
                "INSERT INTO vantage_pending_orders (trade_id,signal_id,channel_name,"
                "direction,price,stop_loss,tps_json,pcts_json,be_at_pos,lot_size,status,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                ("t-rest", "resting", _CH, "BUY", 4178.0, 4174.0, "[]", "[]", 0, 0.01,
                 "working", time.time()))

        assert _replace() == []
        assert _status("resting") == "pending"

    def test_anything_when_the_newer_post_did_not_become_a_signal(self, fresh_db):
        """Skipped (paused, filtered, out of session): it has no signal_id, and
        replacing a live signal with nothing would just drop the setup."""
        _signal("old")
        with db.db() as conn:
            conn.execute(
                "INSERT INTO vantage_tg_signals (tg_message_id,group_id,raw_text,"
                "parsed_at,status) VALUES (?,?,?,?,?)",
                ("22557", "g1", "x", time.time(), "skipped"))

        assert _replace() == []
        assert _status("old") == "pending"
