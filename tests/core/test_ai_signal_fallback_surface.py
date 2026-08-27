"""Proves backend.src.services.trading.ai_signal_fallback's extracted functions
behave identically to SimulationEngine's originals, characterized in
test_ai_signal_fallback_characterization.py -- see
docs/todo/refactor/core-ai-signal-fallback-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. NO real or demo MT5 order is ever placed, closed, or modified --
verified via the fake bridge's own call log.
"""
import asyncio
import os
import tempfile
import time
from unittest import mock

import pytest

from backend.src.services.ai import signal_extractor as ai_signal_extractor
from backend.src.services.ai import claude_ai as claude_ai
from backend.src.db import database as db
from backend.src.services.trading import ai_signal_fallback as fb
from tests._fakes import _FakeBridge


class _RaisingBridge:
    async def modify_order(self, ticket, sl=None, tp=None):
        raise RuntimeError("mt5 unavailable")


class _RejectingBridge:
    """The broker refuses the modify. Critically this is reported as a
    RETURNED {"error": ...} dict, not a raised exception -- the shape that
    made rejected SL moves look successful (see apply_sl_adjustment)."""

    def __init__(self, error="Modify failed: 10016"):
        self.modify_order_calls = []
        self._error = error

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_order_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"error": self._error}


def _insert_open_trade(trade_id="t-1", tg_source="GD VIP", mt5_ticket=555, stop_loss=2390.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-1", "BUY", 2399.0, 2401.0, 2380.0, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "sig-1", mt5_ticket, "BUY", 2399.0, 2401.0, 2400.0, 0.10, 0.10, stop_loss,
             "open", time.time(), tg_source),
        )


# ── try_ai_signal_fallback ─────────────────────────────────────────────────

def test_not_active_trader_node_returns_none_no_dedup_recorded(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, False, _FakeBridge(),
        ))
    assert result is None
    clf.assert_not_called()
    assert db.has_ai_fallback_check("tg-1", "hello") is False


def test_already_dedup_checked_returns_none_no_ai_call(fresh_db):
    db.record_ai_fallback_check("tg-1", "hello")
    with mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    clf.assert_not_called()


def test_non_xauusd_currency_returns_none_no_ai_call(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message") as clf:
        result = asyncio.run(fb.try_ai_signal_fallback(
            "Currency: EURUSD entering now", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    clf.assert_not_called()


def test_ai_call_raises_returns_none_dedup_not_recorded(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message", side_effect=RuntimeError("boom")):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    assert db.has_ai_fallback_check("tg-1", "hello") is False


def test_ai_call_returns_none_dedup_is_recorded(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message", return_value=None):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "hello", "Chan", "tg-1", {}, True, _FakeBridge(),
        ))
    assert result is None
    assert db.has_ai_fallback_check("tg-1", "hello") is True


def test_ai_call_sl_adjustment_applies_and_returns_none(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    ai_result = {
        "kind": "sl_adjustment", "new_stop_loss": 2395.0,
        "_ai_confidence": 0.9, "_ai_reasoning": "moved SL per message",
    }
    bridge = _FakeBridge()
    with mock.patch.object(ai_signal_extractor, "classify_message", return_value=ai_result):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "adjust sl to 2395", "Chan", "tg-1", {}, True, bridge,
        ))

    assert result is None
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0
    with db.db() as conn:
        ai_row = conn.execute(
            "SELECT message_type, new_stop_loss FROM ai_recovered_signals WHERE tg_message_id=?", ("tg-1",)
        ).fetchone()
    assert ai_row[0] == "sl_adjustment"
    assert ai_row[1] == 2395.0


def test_ai_call_ordinary_signal_returns_result_dict(fresh_db):
    ai_result = {
        "direction": "BUY", "entry_low": 2399.0, "entry_high": 2401.0, "stop_loss": 2390.0,
        "tp1": 2405.0, "_ai_confidence": 0.85, "_ai_reasoning": "recovered from chatter",
    }
    with mock.patch.object(ai_signal_extractor, "classify_message", return_value=ai_result):
        result = asyncio.run(fb.try_ai_signal_fallback(
            "buy gold now", "Chan", "tg-2", {}, True, _FakeBridge(),
        ))

    assert result == ai_result
    with db.db() as conn:
        ai_row = conn.execute(
            "SELECT direction, tg_message_id FROM ai_recovered_signals WHERE tg_message_id=?", ("tg-2",)
        ).fetchone()
    assert ai_row[0] == "BUY"


# ── apply_sl_adjustment ────────────────────────────────────────────────────────

def test_apply_sl_adjustment_no_matching_trade_is_noop(fresh_db):
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))
    assert bridge.modify_order_calls == []


def test_apply_sl_adjustment_already_matches_current_sl_is_noop(fresh_db):
    _insert_open_trade(tg_source="Chan", stop_loss=2395.005)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))
    assert bridge.modify_order_calls == []


def test_apply_sl_adjustment_applies_cleanly(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))

    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0


def test_apply_sl_adjustment_no_ticket_updates_db_skips_bridge(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=None, stop_loss=2390.0)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))

    assert bridge.modify_order_calls == []
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2395.0


def test_apply_sl_adjustment_dedup_already_claimed_is_noop(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    db.try_claim_sl_adjustment("tg-1", "Chan", 2395.0)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", bridge))

    assert bridge.modify_order_calls == []
    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2390.0


def test_apply_sl_adjustment_bridge_raises_no_db_write(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "ai_fallback", _RaisingBridge()))

    with db.db() as conn:
        row = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",)).fetchone()
    assert row[0] == 2390.0


# ── queue_unrecognised / analyse_unrecognised_message ─────────────────────────

async def _call_queue_unrecognised(tg_id, channel_name, text):
    with mock.patch.object(fb, "analyse_unrecognised_message", new=mock.AsyncMock()) as m:
        fb.queue_unrecognised(tg_id, channel_name, text, {})
        await asyncio.sleep(0)
        return m


def test_queue_unrecognised_new_message_saves_row_and_schedules_analysis(fresh_db):
    m = asyncio.run(_call_queue_unrecognised("tg-9", "Chan", "??? what is this"))
    with db.db() as conn:
        row = conn.execute(
            "SELECT * FROM channel_unrecognised_messages WHERE tg_message_id=?", ("tg-9",)
        ).fetchone()
    assert row is not None
    m.assert_called_once()


def test_queue_unrecognised_already_queued_is_noop(fresh_db):
    db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    m = asyncio.run(_call_queue_unrecognised("tg-9", "Chan", "??? what is this"))
    with db.db() as conn:
        rows = conn.execute(
            "SELECT * FROM channel_unrecognised_messages WHERE tg_message_id=?", ("tg-9",)
        ).fetchall()
    assert len(rows) == 1
    m.assert_not_called()


def test_analyse_unrecognised_message_success_updates_row(fresh_db):
    unrec_id = db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    analysis = {"is_signal": False, "summary": "just chatter"}
    with mock.patch.object(claude_ai, "classify_unknown_message", return_value=analysis), \
         mock.patch("backend.src.services.telegram.alerts.send_message", new=mock.AsyncMock()):
        asyncio.run(fb.analyse_unrecognised_message(unrec_id, "Chan", "??? what is this", {}))

    with db.db() as conn:
        row = conn.execute(
            "SELECT claude_analysis FROM channel_unrecognised_messages WHERE id=?", (unrec_id,)
        ).fetchone()
    assert "just chatter" in row[0]


def test_analyse_unrecognised_message_exception_still_updates_row(fresh_db):
    unrec_id = db.save_unrecognised_message("Chan", "tg-9", "??? what is this")
    with mock.patch.object(claude_ai, "classify_unknown_message", side_effect=RuntimeError("ai down")), \
         mock.patch("backend.src.services.telegram.alerts.send_message", new=mock.AsyncMock()):
        asyncio.run(fb.analyse_unrecognised_message(unrec_id, "Chan", "??? what is this", {}))

    with db.db() as conn:
        row = conn.execute(
            "SELECT claude_analysis FROM channel_unrecognised_messages WHERE id=?", (unrec_id,)
        ).fetchone()
    assert "ai down" in row[0]


def test_apply_sl_adjustment_broker_rejection_does_not_touch_db(fresh_db):
    """Regression -- live 2026-07-28, ticket 1663956102. A RISK FREE/BE
    instruction arrived after price had run past breakeven, so the stop was
    on the wrong side of the market and MT5 rejected it with "Invalid
    stops". modify_order reports that by RETURNING {"error": ...}, so the
    surrounding try/except never fired: the DB was updated and an "SL
    adjusted" alert sent for a stop the broker never accepted, leaving the
    position on its original, much wider risk with nobody aware."""
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    bridge = _RejectingBridge(error="Modify failed: 10016")
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "logic_keyword", bridge))

    # The attempt is still made -- it is the bookkeeping that must not lie.
    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute(
            "SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",),
        ).fetchone()
    assert row[0] == 2390.0, "DB must keep the real, unmoved SL on rejection"


def _run_and_drain(coro_fn):
    """apply_sl_adjustment fires its Telegram alert via asyncio.create_task,
    so the task needs the loop to tick once more before it has actually
    run -- asyncio.run() only awaits the main coroutine."""
    async def _outer():
        await coro_fn()
        for _ in range(3):
            await asyncio.sleep(0)
    asyncio.run(_outer())


def test_apply_sl_adjustment_rejection_alerts_that_sl_did_not_move(fresh_db):
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    bridge = _RejectingBridge()
    with mock.patch.object(fb.telegram_alerts, "send_message", side_effect=_capture):
        _run_and_drain(lambda: fb.apply_sl_adjustment(
            2395.0, "Chan", "tg-1", "logic_keyword", bridge,
        ))

    assert sent, "a rejection must still be reported, not swallowed"
    body = sent[0]
    assert "REJECTED" in body
    assert "NOT moved" in body
    assert "10016" in body


def test_apply_sl_adjustment_success_still_updates_db_and_alerts(fresh_db):
    """The happy path must be unchanged by the rejection guard."""
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    sent = []

    async def _capture(msg, *a, **k):
        sent.append(msg)

    bridge = _FakeBridge()
    with mock.patch.object(fb.telegram_alerts, "send_message", side_effect=_capture):
        _run_and_drain(lambda: fb.apply_sl_adjustment(
            2395.0, "Chan", "tg-1", "logic_keyword", bridge,
        ))

    with db.db() as conn:
        row = conn.execute(
            "SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",),
        ).fetchone()
    assert row[0] == 2395.0
    assert sent and "REJECTED" not in sent[0]


# ── Self-managed strategies reject channel-pushed SL overrides ─────────────

def _set_strategy(trade_id, strategy):
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET strategy=? WHERE trade_id=?",
                     (strategy, trade_id))


@pytest.mark.parametrize("strategy", sorted(
    __import__("backend.src.utils.models", fromlist=["x"]).STRATEGIES_OWN_SL))
def test_own_sl_strategies_ignore_channel_pushed_sl(fresh_db, strategy):
    """Fixed R:R's 4pt stop is the measured output of the exit-policy lab,
    and SL-tightening reduced expectancy in 14/14 configurations tested.
    A channel message ("SL IS SET TO BE AT 4021") must not silently replace
    it mid-trade. Same reasoning for the other fill-relative strategies."""
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    _set_strategy("t-1", strategy)
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "logic_keyword", bridge))

    assert bridge.modify_order_calls == [], "must not touch the broker"
    with db.db() as conn:
        row = conn.execute(
            "SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",),
        ).fetchone()
    assert row[0] == 2390.0, "strategy's own stop must survive"


def test_signal_following_strategies_still_accept_channel_sl(fresh_db):
    """The exclusion must be narrow -- strategies that trade the channel's
    own levels should keep following the channel's updates."""
    _insert_open_trade(tg_source="Chan", mt5_ticket=555, stop_loss=2390.0)
    _set_strategy("t-1", "signal_climber")
    bridge = _FakeBridge()
    asyncio.run(fb.apply_sl_adjustment(2395.0, "Chan", "tg-1", "logic_keyword", bridge))

    assert bridge.modify_order_calls == [{"ticket": 555, "sl": 2395.0, "tp": None}]
    with db.db() as conn:
        row = conn.execute(
            "SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?", ("t-1",),
        ).fetchone()
    assert row[0] == 2395.0
