"""Proves forex_trader.core.core_scan_messages_parse_classify.classify_and_parse
behaves identically to SimulationEngine's original, characterized in
test_scan_messages_parse_classify_characterization.py -- see
docs/todo/refactor/core-scan-messages-parse-classify-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. Parsing/classification and DB recording only -- no MT5 order is
ever placed, closed, or modified.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timezone
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import telegram_alerts
from forex_trader.core import core_scan_messages_parse_classify as pc
from forex_trader.core.signal_parser import SIGNAL_PREFIX


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


def _get_tg_row(tg_id):
    with db.db() as conn:
        r = conn.execute("SELECT * FROM vantage_tg_signals WHERE tg_message_id=?", (tg_id,)).fetchone()
        return db.row_to_dict(r) if r else None


# Evaluated per call, not at import. As a module-level constant this was
# fixed at collection time, so by the time these tests ran near the end of
# a ~6 minute suite the "fresh" timestamp was older than the production
# staleness threshold (_MAX_SIGNAL_AGE_SECS = 4 minutes) and the signal was
# correctly rejected as stale. Passed in isolation, failed in the full run.
def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()

_GD2_FULL = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\nTP4 4543\nTP5 4545\n\nSL 4527"
_GD2_PARTIAL = "XAU USD BUY NOW\n4534 - 4529"
_GD2_BARE = "XAU USD BUY NOW"
_FORMAT_A = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
             "Sell Gold 4520 - 4512\nStop Loss 4524\nTP1 4510  TP2 4508  TP3 4506  TP4 4503  TP5 4500")

_AI_RESULT = {"direction": "BUY", "entry_low": 4529.0, "entry_high": 4534.0, "stop_loss": 4527.0,
              "tp1": 4537.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
              "tp6": None, "tp7": None, "tp8": None}


def _call(tg_id, text, parser_fmt, sig_prefix="", channel_name="TestChannel", group_id="g1",
         ai_return=None, force_gd2_all_fail=False, learned_rules_return=None, rs=None,
         ai_calls=None):
    msg = {"id": tg_id, "group_id": group_id, "text": text, "timestamp": _now_iso(),
          "sender_name": "sender"}
    uq_calls = []
    alerts = []

    async def fake_ai(text_, channel_name_, tg_id_):
        if ai_calls is not None:
            ai_calls.append(tg_id_)
        return ai_return

    def fake_queue(tg_id_, channel_name_, text_):
        uq_calls.append((tg_id_, channel_name_))

    async def fake_send(msg_, *a, **k):
        alerts.append(msg_)

    patches = [mock.patch.object(telegram_alerts, "send_message", side_effect=fake_send)]
    if force_gd2_all_fail:
        patches += [
            mock.patch.object(pc, "parse_gd2_signal", return_value=None),
            mock.patch.object(pc, "parse_gd2_partial", return_value=None),
        ]
        import forex_trader.core.signal_parser as sp
        patches.append(mock.patch.object(sp, "parse_gd2_instant_entry", return_value=None))
    if learned_rules_return is not None:
        patches.append(mock.patch.object(pc, "parse_with_learned_rules", return_value=learned_rules_return))
    for p in patches:
        p.start()
    try:
        result = asyncio.run(pc.classify_and_parse(
            tg_id, group_id, channel_name, text, msg, parser_fmt, sig_prefix,
            fake_ai, fake_queue, rs if rs is not None else {},
        ))
    finally:
        for p in patches:
            p.stop()
    return result, uq_calls, alerts


def test_learned_rules_short_circuit(fresh_db):
    learned = {"direction": "SELL", "entry_low": 100.0, "entry_high": 101.0, "stop_loss": 102.0,
               "tp1": 99.0, "tp2": None, "tp3": None, "tp4": None, "tp5": None,
               "tp6": None, "tp7": None, "tp8": None}
    result, uq, alerts = _call("b1", "anything at all", "format_ab", learned_rules_return=learned)
    assert result is not None
    assert result["direction"] == "SELL"
    assert result["entry_low"] == 100.0


def test_format_ab_no_match_ai_fails_dropped(fresh_db):
    result, uq, alerts = _call("b2", "totally unrelated chatter", "format_ab", ai_return=None)
    assert result is None
    assert uq == []


def test_format_ab_no_match_ai_recovers(fresh_db):
    result, uq, alerts = _call("b3", "random gold buy chatter AI recovers", "format_ab", ai_return=_AI_RESULT)
    assert result is not None
    assert result["direction"] == "BUY"


def test_format_ab_currency_mismatch_recorded_and_alerted(fresh_db):
    text = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
            "Currency: EURUSD\nBuy Gold 1.1200 - 1.1180\nStop Loss 1.1150\nTP1 1.1250")
    result, uq, alerts = _call("b4", text, "format_ab")
    assert result is None
    row = _get_tg_row("b4")
    assert row["status"] == "unsupported_currency"
    assert len(alerts) == 1


def test_format_ab_currency_mismatch_dedup_window_suppresses_second_alert(fresh_db):
    text1 = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
             "Direction  BUY\nCurrency: EURUSD\nENTRY : 1.1180-1.1200\nTP1: 1.1250\nSL: 1.1150")
    text2 = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
             "Direction  BUY\nCurrency: EURUSD\nENTRY : 1.1190-1.1210\nTP1: 1.1260\nSL: 1.1160")
    _call("dup1", text1, "format_ab")
    _, _, alerts2 = _call("dup2", text2, "format_ab")
    assert alerts2 == []


def test_format_ab_matches_parses_cleanly(fresh_db):
    result, uq, alerts = _call("b5", _FORMAT_A, "format_ab")
    assert result is not None
    assert result["direction"] == "SELL"


def test_format_ab_matches_parse_fails_ai_fails_queues_unrecognised(fresh_db):
    bad_text = ("This is not financial advice. Use appropriate risk management if you're going to trade.\n"
                "Direction BUY\nENTRY: 4510-4508\n")
    result, uq, alerts = _call("b6", bad_text, "format_ab", ai_return=None)
    assert result is None
    assert uq == [("b6", "TestChannel")]


def test_gd2_no_match_ai_fails_dropped(fresh_db):
    result, uq, alerts = _call("b8", "not a gold signal at all", "gd2", ai_return=None)
    assert result is None
    assert uq == []


def test_gd2_no_match_ai_recovers(fresh_db):
    result, uq, alerts = _call("b9", "random gold buy chatter AI recovers as gd2", "gd2", ai_return=_AI_RESULT)
    assert result is not None
    assert result["direction"] == "BUY"


def test_gd2_full_parse_succeeds(fresh_db):
    result, uq, alerts = _call("b10", _GD2_FULL, "gd2")
    assert result is not None
    assert result["direction"] == "BUY"


def test_gd2_partial_records_pending_followup(fresh_db):
    result, uq, alerts = _call("b11", _GD2_PARTIAL, "gd2")
    assert result is None
    row = _get_tg_row("b11")
    assert row["status"] == "pending_followup"


def test_gd2_bare_instant_trigger_silently_skipped(fresh_db):
    result, uq, alerts = _call("b12", _GD2_BARE, "gd2")
    assert result is None
    assert uq == []


def test_gd2_all_parsers_fail_ai_fails_queues_unrecognised(fresh_db):
    text = "XAU USD BUY NOW garbled unrecognisable content"
    result, uq, alerts = _call("b13", text, "gd2", ai_return=None, force_gd2_all_fail=True)
    assert result is None
    assert uq == [("b13", "TestChannel")]


def test_gd2_all_parsers_fail_ai_recovers(fresh_db):
    text = "XAU USD BUY NOW garbled unrecognisable content"
    result, uq, alerts = _call("b14", text, "gd2", ai_return=_AI_RESULT, force_gd2_all_fail=True)
    assert result is not None
    assert result["direction"] == "BUY"


def test_auto_format_ab_matches_first(fresh_db):
    # sig_prefix is resolved by the caller as `ch_cfg.get('signal_prefix') or
    # SIGNAL_PREFIX` before this function runs -- it's never actually empty.
    result, uq, alerts = _call("b15", _FORMAT_A, "auto", sig_prefix=SIGNAL_PREFIX)
    assert result is not None
    assert result["direction"] == "SELL"


def test_auto_falls_to_gd2_full_parse(fresh_db):
    result, uq, alerts = _call("b16", _GD2_FULL, "auto", sig_prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH")
    assert result is not None
    assert result["direction"] == "BUY"


def test_auto_gd2_partial_records_pending_followup(fresh_db):
    result, uq, alerts = _call("b17", _GD2_PARTIAL, "auto", sig_prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH")
    assert result is None
    row = _get_tg_row("b17")
    assert row["status"] == "pending_followup"


def test_auto_neither_matches_ai_fails_silently_dropped(fresh_db):
    result, uq, alerts = _call("b18", "totally unrelated text", "auto",
                               sig_prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH", ai_return=None)
    assert result is None
    assert uq == []


def test_auto_neither_matches_ai_recovers(fresh_db):
    result, uq, alerts = _call("b19", "totally unrelated gold buy text AI recovers", "auto",
                               sig_prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH", ai_return=_AI_RESULT)
    assert result is not None
    assert result["direction"] == "BUY"


# ── Logic Keywords: symbol_tokens/buy_orders/limit_orders AI-fallback gate ──
# 2026-07-24: these three lexicon categories were pure reference lists with
# zero effect on live parsing until this fix -- see
# core_logic_keyword_triggers.should_skip_ai_fallback_for_no_signal_candidate.

def test_ai_fallback_skipped_when_no_candidate_keyword_present(fresh_db):
    calls = []
    result, uq, alerts = _call(
        "c1", "friday market wrap up nothing exciting happened today", "auto",
        sig_prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH", ai_return=_AI_RESULT, ai_calls=calls,
    )
    assert result is None
    assert calls == []  # AI never even invoked -- gate short-circuited it


def test_ai_fallback_proceeds_when_symbol_token_present(fresh_db):
    calls = []
    result, uq, alerts = _call(
        "c2", "unusual wording but mentions GOLD somewhere", "auto",
        sig_prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH", ai_return=_AI_RESULT, ai_calls=calls,
    )
    assert result is not None
    assert calls == ["c2"]


def test_ai_fallback_gate_disabled_when_all_three_lexicons_emptied(fresh_db):
    from forex_trader.core import core_logic_keywords as logic_kw
    logic_kw.set_lexicon("symbol_tokens", [])
    logic_kw.set_lexicon("buy_orders", [])
    logic_kw.set_lexicon("limit_orders", [])
    calls = []
    result, uq, alerts = _call(
        "c3", "no trading vocabulary here at all", "auto",
        sig_prefix="SPECIFIC_PREFIX_THAT_WONT_MATCH", ai_return=_AI_RESULT, ai_calls=calls,
    )
    assert result is not None
    assert calls == ["c3"]
