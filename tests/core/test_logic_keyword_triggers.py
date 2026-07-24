"""core_logic_keyword_triggers.py: CLOSE ALL / RISK FREE-BE / TP HIT
triggers and the media/forwarded/exclusion pre-filters, wired into
engine.py's _scan_messages ahead of the normal signal-classification
pipeline. Real-money surface: try_handle_close_all_trigger calls a
close_trade_fn double (never the real core_close_trade.close_trade) and
try_handle_risk_free_be_trigger delegates to core_ai_signal_fallback.
apply_sl_adjustment against a fake bridge -- no real or demo MT5 order is
ever placed, closed, or modified here.
"""
import asyncio
import os
import tempfile
import time

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_logic_keywords as lk
from forex_trader.core import core_logic_keyword_triggers as trig


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


_RS_ALL_ON = {
    "lk_enable_close_all_parsing": 1, "lk_enable_risk_free_be_parsing": 1,
    "lk_enable_tp_hit_parsing": 1, "lk_ignore_media_messages": 1,
    "lk_ignore_forwarded_messages": 0,
}


def _insert_open_trade(trade_id="t1", channel="Gold Diggers VIP", entry_price=4100.0,
                       stop_loss=4090.0, tg_source=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,entry_high,"
            "stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-1", channel, "BUY", entry_price, entry_price, stop_loss, "active", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,direction,entry_low,"
            "entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,open_time,tg_source) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, "sig-1", "BUY", entry_price, entry_price, entry_price, 0.10, 0.10,
             stop_loss, "open", time.time(), tg_source if tg_source is not None else channel),
        )


# ── should_skip_media_or_forwarded ──────────────────────────────────────────

def test_ignore_media_when_toggle_on_and_has_media():
    reason = trig.should_skip_media_or_forwarded({"has_media": True}, {"lk_ignore_media_messages": 1})
    assert reason is not None


def test_media_message_kept_when_toggle_off():
    reason = trig.should_skip_media_or_forwarded({"has_media": True}, {"lk_ignore_media_messages": 0})
    assert reason is None


def test_ignore_forwarded_when_toggle_on():
    reason = trig.should_skip_media_or_forwarded({"forwarded": True}, {"lk_ignore_forwarded_messages": 1})
    assert reason is not None


def test_forwarded_kept_by_default_toggle_off():
    reason = trig.should_skip_media_or_forwarded({"forwarded": True}, {})
    assert reason is None


def test_plain_message_never_skipped():
    assert trig.should_skip_media_or_forwarded({"has_media": False, "forwarded": False}, _RS_ALL_ON) is None


# ── should_skip_for_exclusion ───────────────────────────────────────────────

def test_exclusion_keyword_skips(fresh_db):
    assert trig.should_skip_for_exclusion("Daily RESULT summary", {}) is not None


def test_no_exclusion_keyword_proceeds(fresh_db):
    assert trig.should_skip_for_exclusion("Buy Gold 4100-4095", {}) is None


def test_no_symbol_mention_still_proceeds(fresh_db):
    """Deliberately NOT gated on symbol_tokens here -- see the function's own
    docstring for the regression this avoids (deterministic-format signals
    that never literally say GOLD/XAU). symbol_tokens IS wired elsewhere --
    see should_skip_ai_fallback_for_no_signal_candidate below."""
    assert trig.should_skip_for_exclusion("random chatter with no symbol at all", {}) is None


# ── should_skip_ai_fallback_for_no_signal_candidate ─────────────────────────

def test_ai_fallback_gate_skips_with_no_candidate_keyword(fresh_db):
    reason = trig.should_skip_ai_fallback_for_no_signal_candidate(
        "nothing relevant here at all", {},
    )
    assert reason is not None


def test_ai_fallback_gate_proceeds_on_symbol_token(fresh_db):
    assert trig.should_skip_ai_fallback_for_no_signal_candidate("mentions GOLD somewhere", {}) is None


def test_ai_fallback_gate_proceeds_on_buy_orders_token(fresh_db):
    assert trig.should_skip_ai_fallback_for_no_signal_candidate("looks like a BUY setup", {}) is None


def test_ai_fallback_gate_proceeds_on_limit_orders_token(fresh_db):
    assert trig.should_skip_ai_fallback_for_no_signal_candidate("check the AREA below", {}) is None


def test_ai_fallback_gate_disabled_when_all_lexicons_empty(fresh_db):
    lk.set_lexicon("symbol_tokens", [])
    lk.set_lexicon("buy_orders", [])
    lk.set_lexicon("limit_orders", [])
    assert trig.should_skip_ai_fallback_for_no_signal_candidate("literally anything", {}) is None


# ── try_handle_close_all_trigger ────────────────────────────────────────────

async def _fake_close_trade(trade_id, reason):
    _fake_close_trade.calls.append((trade_id, reason))
    return {"trade_id": trade_id}
_fake_close_trade.calls = []


def test_close_all_disabled_toggle_returns_false(fresh_db):
    _fake_close_trade.calls = []
    handled = asyncio.run(trig.try_handle_close_all_trigger(
        "CLOSE ALL", "Gold Diggers VIP", "tg1", {"lk_enable_close_all_parsing": 0},
        close_trade_fn=_fake_close_trade,
    ))
    assert handled is False
    assert _fake_close_trade.calls == []


def test_close_all_no_phrase_match_returns_false(fresh_db):
    _fake_close_trade.calls = []
    handled = asyncio.run(trig.try_handle_close_all_trigger(
        "just chatting", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
        close_trade_fn=_fake_close_trade,
    ))
    assert handled is False


def test_close_all_matches_and_closes_channel_trade(fresh_db):
    _fake_close_trade.calls = []
    _insert_open_trade(trade_id="t1", channel="Gold Diggers VIP")
    handled = asyncio.run(trig.try_handle_close_all_trigger(
        "CLOSE ALL please", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
        close_trade_fn=_fake_close_trade,
    ))
    assert handled is True
    assert _fake_close_trade.calls == [("t1", "logic_keyword_close_all")]


def test_close_all_does_not_close_a_different_channels_trade(fresh_db):
    _fake_close_trade.calls = []
    _insert_open_trade(trade_id="t1", channel="Some Other Channel")
    handled = asyncio.run(trig.try_handle_close_all_trigger(
        "CLOSE ALL", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
        close_trade_fn=_fake_close_trade,
    ))
    assert handled is True  # phrase matched -- still "handled" (no further parsing)
    assert _fake_close_trade.calls == []  # but nothing closed -- wrong channel


def test_close_all_dedup_second_message_no_op(fresh_db):
    _fake_close_trade.calls = []
    _insert_open_trade(trade_id="t1", channel="Gold Diggers VIP")
    asyncio.run(trig.try_handle_close_all_trigger(
        "CLOSE ALL", "Gold Diggers VIP", "tg1", _RS_ALL_ON, close_trade_fn=_fake_close_trade,
    ))
    asyncio.run(trig.try_handle_close_all_trigger(
        "CLOSE ALL", "Gold Diggers VIP", "tg1", _RS_ALL_ON, close_trade_fn=_fake_close_trade,
    ))
    assert len(_fake_close_trade.calls) == 1


# ── try_handle_risk_free_be_trigger ─────────────────────────────────────────

class _FakeBridge:
    def __init__(self):
        self.modify_calls = []

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        return {"success": True}


def test_risk_free_be_disabled_toggle_returns_false(fresh_db):
    handled = asyncio.run(trig.try_handle_risk_free_be_trigger(
        "RISK FREE now", "Gold Diggers VIP", "tg1",
        {"lk_enable_risk_free_be_parsing": 0}, bridge=_FakeBridge(),
    ))
    assert handled is False


def test_risk_free_be_matches_moves_sl_to_entry(fresh_db):
    _insert_open_trade(trade_id="t1", channel="Gold Diggers VIP", entry_price=4100.0, stop_loss=4090.0)
    bridge = _FakeBridge()
    handled = asyncio.run(trig.try_handle_risk_free_be_trigger(
        "please set BE now for risk free", "Gold Diggers VIP", "tg1", _RS_ALL_ON, bridge=bridge,
    ))
    assert handled is True
    with db.db() as conn:
        sl = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id='t1'").fetchone()[0]
    assert sl == 4100.0


def test_risk_free_be_no_match_returns_false(fresh_db):
    handled = asyncio.run(trig.try_handle_risk_free_be_trigger(
        "just chatting", "Gold Diggers VIP", "tg1", _RS_ALL_ON, bridge=_FakeBridge(),
    ))
    assert handled is False


def test_risk_free_be_no_open_trade_still_handled_no_crash(fresh_db):
    handled = asyncio.run(trig.try_handle_risk_free_be_trigger(
        "RISK FREE", "Nonexistent Channel", "tg1", _RS_ALL_ON, bridge=_FakeBridge(),
    ))
    assert handled is True


def test_risk_free_be_no_trade_still_claims_dedup(fresh_db):
    """Regression: previously, a RISK FREE/BE match with no open trade never
    claimed the dedup guard at all, so the same buffered message re-fired
    every scan cycle -- and worse, would have applied to a DIFFERENT trade
    that opened later on the same channel while the old message was still
    buffered. Confirmed via claim_trigger's own idempotency: a second claim
    attempt for the same (tg_id, trigger_type) must now fail."""
    asyncio.run(trig.try_handle_risk_free_be_trigger(
        "RISK FREE", "Nonexistent Channel", "tg1", _RS_ALL_ON, bridge=_FakeBridge(),
    ))
    assert lk.claim_trigger("tg1", "risk_free_be") is False


def test_risk_free_be_second_scan_of_same_message_does_not_reapply(fresh_db):
    """A trade opens AFTER the first (no-op) scan of an old buffered
    message -- the second scan of that same tg_id must not retroactively
    apply BE to the new trade."""
    asyncio.run(trig.try_handle_risk_free_be_trigger(
        "RISK FREE", "Gold Diggers VIP", "tg1", _RS_ALL_ON, bridge=_FakeBridge(),
    ))
    _insert_open_trade(trade_id="t1", channel="Gold Diggers VIP", entry_price=4100.0, stop_loss=4090.0)
    asyncio.run(trig.try_handle_risk_free_be_trigger(
        "RISK FREE", "Gold Diggers VIP", "tg1", _RS_ALL_ON, bridge=_FakeBridge(),
    ))
    with db.db() as conn:
        sl = conn.execute("SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id='t1'").fetchone()[0]
    assert sl == 4090.0  # unchanged -- the claim already happened on the first (trade-less) scan


# ── try_handle_tp_hit_trigger ───────────────────────────────────────────────

def test_tp_hit_disabled_toggle_returns_false(fresh_db):
    handled = asyncio.run(trig.try_handle_tp_hit_trigger(
        "TP2 HITT +40pips", "Gold Diggers VIP", "tg1", {"lk_enable_tp_hit_parsing": 0},
    ))
    assert handled is False


def test_tp_hit_matches_pattern(fresh_db):
    handled = asyncio.run(trig.try_handle_tp_hit_trigger(
        "TP2 HITT +40pips \U0001f911", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
    ))
    assert handled is True


def test_tp_hit_no_action_on_trade(fresh_db):
    """Confirmed with the user: TP HIT is log/notify only -- must not touch
    any trade's SL or status."""
    _insert_open_trade(trade_id="t1", channel="Gold Diggers VIP", stop_loss=4090.0)
    asyncio.run(trig.try_handle_tp_hit_trigger(
        "TP1 HIT +30pips", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
    ))
    with db.db() as conn:
        row = conn.execute(
            "SELECT stop_loss, status FROM vantage_simulated_trades WHERE trade_id='t1'"
        ).fetchone()
    assert tuple(row) == (4090.0, "open")


def test_tp_hit_no_match_returns_false(fresh_db):
    handled = asyncio.run(trig.try_handle_tp_hit_trigger(
        "just chatting", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
    ))
    assert handled is False


def test_tp_hit_dedup_second_message_still_true_but_no_duplicate_alert(fresh_db):
    handled1 = asyncio.run(trig.try_handle_tp_hit_trigger(
        "TP1 HIT", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
    ))
    handled2 = asyncio.run(trig.try_handle_tp_hit_trigger(
        "TP1 HIT", "Gold Diggers VIP", "tg1", _RS_ALL_ON,
    ))
    assert handled1 is True
    assert handled2 is True  # claim_trigger fails silently, still "handled" (no re-parse as signal)
