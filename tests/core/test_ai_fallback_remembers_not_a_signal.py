"""The AI fallback does not pay twice to be told the same text is not a
signal (bugs/053, option 1).

`ai_fallback_checked` keys on (tg_message_id, text), so a channel that posts
the same heads-up ("PREPARE FOR BUY LIMITS") before every setup paid for a
fresh classification each time: 41 calls for 2 distinct texts in three days,
none of which recovered anything.

Now the VERDICT is remembered, and only the negative one: a text the AI has
said is not a signal (and not an SL adjustment) is not sent again under a new
message id. A text that once produced a signal or an SL adjustment is always
asked again, so a channel re-posting a real signal never loses it.

No AI provider is called: classify_message is a mock.
"""
import asyncio
from unittest import mock

from backend.src.services.ai import signal_extractor as ai_signal_extractor
from backend.src.services.trading import ai_signal_fallback as fb
from tests._fakes import _FakeBridge

HEADS_UP = "PREPARE FOR BUY LIMITS"


def _run(text, tg_id, result):
    with mock.patch.object(ai_signal_extractor, "classify_message",
                           return_value=result) as clf:
        out = asyncio.run(fb.try_ai_signal_fallback(text, "Chan", tg_id, {}, True, _FakeBridge()))
    return out, clf.call_count


def test_the_same_chatter_under_a_new_message_id_is_not_paid_for_again(fresh_db):
    assert _run(HEADS_UP, "tg-1", None) == (None, 1)
    assert _run(HEADS_UP, "tg-2", None) == (None, 0)


def test_different_text_is_still_asked(fresh_db):
    _run(HEADS_UP, "tg-1", None)
    assert _run("PREPARE FOR SELL LIMITS", "tg-2", None)[1] == 1


def test_a_text_that_produced_a_signal_is_always_asked_again(fresh_db):
    text = "XAUUSD BUY 4350 SL 4345 TP 4360"
    signal = {"kind": "signal", "direction": "BUY", "entry_low": 4350.0,
              "entry_high": 4350.0, "stop_loss": 4345.0, "tp1": 4360.0,
              "_ai_confidence": 0.9, "_ai_reasoning": "clear"}
    _run(text, "tg-1", signal)
    assert _run(text, "tg-2", signal)[1] == 1


def test_a_failed_call_records_no_verdict(fresh_db):
    with mock.patch.object(ai_signal_extractor, "classify_message",
                           side_effect=RuntimeError("timeout")):
        asyncio.run(fb.try_ai_signal_fallback(HEADS_UP, "Chan", "tg-1", {}, True, _FakeBridge()))
    assert _run(HEADS_UP, "tg-2", None)[1] == 1
