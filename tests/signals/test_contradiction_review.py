"""Contradiction review (docs/simon-handover/041, owner 2026-10-11).

"Add into risk settings a contradiction review that uses its ml/ai
capabilities to evaluate the market to determine which one should be
executed."

When a signal arrives and a different source has an opposing signal live on
the bus (within ten minutes, any pairing: channel, engine), the review reads
the market (H1 and H4 trend bias, M15 ADX regime and MACD momentum) and asks
the configured AI provider which side to execute. If the AI cannot answer,
the indicator vote decides. The newcomer is refused when the answer is the
other side or neither.

What it can never do: close, cancel or modify anything already live. It
returns allow/refuse for the signal in hand and nothing else.

Off by default (`contradiction_review_enabled`, migration 59): with it off,
nothing here reads the bus, the market or the AI.

Uses fresh_db for the bus. The bridge is a stub serving canned candles and
the AI provider is replaced; nothing here reaches a broker or the network.
"""
from __future__ import annotations

import asyncio
import json
import time
import types
from pathlib import Path

import pytest

from backend.src.services.signals import contradiction_review as cr

ON = {"contradiction_review_enabled": 1}
OFF = {"contradiction_review_enabled": 0}


def _trend(up: bool, n: int = 80) -> list[dict]:
    out, p = [], 2400.0
    for _ in range(n):
        p += 1.0 if up else -1.0
        out.append({"open": p - 0.5 if up else p + 0.5, "high": p + 0.6,
                    "low": p - 0.6, "close": p})
    return out


class _Bridge:
    def __init__(self, up=None, fail=False):
        self.up, self.fail, self.calls = up, fail, 0

    async def get_candles(self, tf, n):
        self.calls += 1
        if self.fail:
            raise RuntimeError("bridge down")
        if self.up is None:
            return []
        return _trend(self.up, max(n, 80))


@pytest.fixture
def ai(monkeypatch):
    """Replace the provider. `ai.reply` is what it answers; None = not
    configured; an Exception instance = it raises."""
    state = types.SimpleNamespace(reply=None, calls=0, prompts=[])

    async def complete(cfg, system, prompt, max_tokens, timeout=30):
        state.calls += 1
        state.prompts.append(prompt)
        if isinstance(state.reply, Exception):
            raise state.reply
        return state.reply

    monkeypatch.setattr(cr._provider, "complete", complete)
    monkeypatch.setattr(cr._provider, "is_configured", lambda cfg: state.reply is not None)
    monkeypatch.setattr(cr, "_load_cfg", lambda: {})
    cr._cache.clear()
    return state


def _bus(fresh_db, name, direction, kind="telegram", age=0.0):
    rid = fresh_db.write_signal_bus(name, direction, ttl_seconds=1800,
                                    symbol="XAUUSD", source_kind=kind, source_name=name)
    if age:
        with fresh_db.db() as conn:
            conn.execute("UPDATE signal_bus SET created_at=created_at-? WHERE id=?", (age, rid))


def _review(rs, bridge, name="Gold Diggers VIP", direction="BUY", kind="telegram"):
    return asyncio.run(cr.review(rs, bridge, source_kind=kind, source_name=name,
                                 direction=direction, symbol="XAUUSD"))


# ── Off, and nothing to review ───────────────────────────────────────────────

def test_off_reads_nothing_and_allows(fresh_db, ai):
    _bus(fresh_db, "reversal_engine", "SELL", kind="engine")
    bridge = _Bridge(up=False)
    ai.reply = json.dumps({"execute": "SELL"})
    assert _review(OFF, bridge) == (True, "")
    assert bridge.calls == 0 and ai.calls == 0


def test_no_opposing_signal_allows_without_asking(fresh_db, ai):
    _bus(fresh_db, "reversal_engine", "BUY", kind="engine")
    bridge = _Bridge(up=False)
    ai.reply = json.dumps({"execute": "SELL"})
    assert _review(ON, bridge) == (True, "")
    assert bridge.calls == 0 and ai.calls == 0


def test_a_source_does_not_contradict_itself(fresh_db, ai):
    _bus(fresh_db, "Gold Diggers VIP", "SELL")
    ai.reply = json.dumps({"execute": "SELL"})
    assert _review(ON, _Bridge(up=False)) == (True, "")
    assert ai.calls == 0


def test_an_opposing_signal_older_than_the_window_is_ignored(fresh_db, ai):
    _bus(fresh_db, "Other Channel", "SELL", age=cr.WINDOW_S + 5)
    ai.reply = json.dumps({"execute": "SELL"})
    assert _review(ON, _Bridge(up=False)) == (True, "")
    assert ai.calls == 0


# ── The AI decides ───────────────────────────────────────────────────────────

def test_the_ai_choosing_the_newcomer_lets_it_through(fresh_db, ai):
    _bus(fresh_db, "reversal_engine", "SELL", kind="engine")
    ai.reply = json.dumps({"execute": "BUY", "reason": "H4 uptrend"})
    assert _review(ON, _Bridge(up=True)) == (True, "")
    assert ai.calls == 1


def test_the_ai_choosing_the_other_side_refuses_the_newcomer(fresh_db, ai):
    _bus(fresh_db, "reversal_engine", "SELL", kind="engine")
    ai.reply = json.dumps({"execute": "SELL", "reason": "rejection at resistance"})
    ok, why = _review(ON, _Bridge(up=True))
    assert ok is False
    assert "contradiction review" in why
    assert "reversal_engine" in why and "SELL" in why


def test_the_ai_choosing_neither_refuses_the_newcomer(fresh_db, ai):
    _bus(fresh_db, "Other Channel", "SELL")
    ai.reply = json.dumps({"execute": "neither"})
    assert _review(ON, _Bridge(up=True))[0] is False


def test_the_prompt_carries_both_sides_and_the_market(fresh_db, ai):
    _bus(fresh_db, "Other Channel", "SELL")
    ai.reply = json.dumps({"execute": "BUY"})
    _review(ON, _Bridge(up=True))
    prompt = ai.prompts[0]
    assert "Gold Diggers VIP" in prompt and "Other Channel" in prompt
    assert "h1_bias" in prompt and "h4_bias" in prompt and "adx" in prompt


# ── When the AI cannot answer, the indicators do ─────────────────────────────

def test_no_ai_and_the_trend_agrees_with_the_newcomer(fresh_db, ai):
    _bus(fresh_db, "Other Channel", "SELL")
    ai.reply = None
    assert _review(ON, _Bridge(up=True), direction="BUY") == (True, "")


def test_no_ai_and_the_trend_disagrees_with_the_newcomer(fresh_db, ai):
    _bus(fresh_db, "Other Channel", "BUY")
    ai.reply = None
    ok, why = _review(ON, _Bridge(up=True), direction="SELL")
    assert ok is False and "indicators" in why


def test_an_ai_failure_falls_back_to_the_indicators(fresh_db, ai):
    _bus(fresh_db, "Other Channel", "BUY")
    ai.reply = TimeoutError("slow")
    assert _review(ON, _Bridge(up=True), direction="SELL")[0] is False


def test_an_unreadable_ai_reply_falls_back_to_the_indicators(fresh_db, ai):
    _bus(fresh_db, "Other Channel", "BUY")
    ai.reply = "I think gold looks interesting today"
    assert _review(ON, _Bridge(up=True), direction="SELL")[0] is False


def test_no_ai_and_no_market_read_allows(fresh_db, ai):
    """Nothing to decide on: today's behaviour, which is to take it."""
    _bus(fresh_db, "Other Channel", "SELL")
    ai.reply = None
    assert _review(ON, _Bridge(fail=True)) == (True, "")


# ── Never raises, and cheap on a second route ────────────────────────────────

def test_it_never_raises(fresh_db, ai, monkeypatch):
    _bus(fresh_db, "Other Channel", "SELL")
    monkeypatch.setattr(cr, "_opposing", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert _review(ON, _Bridge(up=True)) == (True, "")


def test_the_same_question_twice_asks_the_ai_once(fresh_db, ai):
    """A Telegram signal can pass two gates (scan_auto_execute, then the
    shared open path). The second must not pay for a second AI call."""
    _bus(fresh_db, "Other Channel", "SELL")
    ai.reply = json.dumps({"execute": "SELL"})
    first = _review(ON, _Bridge(up=True))
    second = _review(ON, _Bridge(up=True))
    assert first == second and ai.calls == 1


# ── What it can never do ─────────────────────────────────────────────────────

def test_it_cannot_close_cancel_or_modify_anything():
    src = (Path(cr.__file__)).read_text(encoding="utf-8")
    for forbidden in ("close_trade", "partial_close", "cancel_pending", "cancel_order",
                      "modify_position", "close_bus_entry", "place_pending_order"):
        assert forbidden not in src, forbidden


def test_the_negative_control_for_that_scan():
    src = (Path(cr.__file__)).read_text(encoding="utf-8")
    assert "get_concurrent_signals" in src


# ── An engine named by its display name is still itself ─────────────────────

def test_an_engine_display_name_maps_to_its_bus_name():
    assert cr.source_of("Reversal Engine") == ("engine", "reversal_engine")
    assert cr.source_of("Breakout Engine") == ("engine", "breakout")
    assert cr.source_of("breakout") == ("engine", "breakout")
    assert cr.source_of("Gold Diggers VIP") == ("telegram", "Gold Diggers VIP")


def test_an_engine_that_flipped_is_not_reviewed_against_itself(fresh_db, ai):
    _bus(fresh_db, "reversal_engine", "SELL", kind="engine")
    ai.reply = json.dumps({"execute": "SELL"})
    ok = asyncio.run(cr.review_source(ON, _Bridge(up=False), "Reversal Engine", "BUY"))
    assert ok == (True, "") and ai.calls == 0


# ── Every automated route asks it ────────────────────────────────────────────

ROOT = Path(__file__).resolve().parents[2] / "backend" / "src" / "services"

# The routes that keep their own copy of the trading-schedule gate because
# they never reach resolve_open_trade_params, plus that function itself
# (which the engines, queued zone fills and fill-time activation go through).
ROUTES = (
    "signals/resolution.py",
    "trading/scan_auto_execute.py",
    "trading/instant_entry.py",
    "trading/limit_order_signal.py",
)


@pytest.mark.parametrize("rel", ROUTES)
def test_every_automated_route_asks_the_review(rel):
    assert "review_source(" in (ROOT / rel).read_text(encoding="utf-8"), rel


def test_the_route_list_negative_control():
    """The scan must be able to say no."""
    assert "review_source(" not in (ROOT / "trading/open_from_signal.py").read_text(encoding="utf-8")


@pytest.mark.parametrize("rel", (
    "breakout_signal/breakout_signal_live_execute.py",
    "trend_pa/live_execute.py",
    "signals/pending_activation.py",
    "reversal_engine/reversal_engine_live_execute.py",
))
def test_the_engines_and_fill_time_reach_the_shared_path(rel):
    """Covered by resolution.py's copy rather than their own: each opens
    through open_trade_from_signal or resolve_open_trade_params."""
    src = (ROOT / rel).read_text(encoding="utf-8")
    assert "open_trade_from_signal(" in src or "resolve_open_trade_params(" in src


# ── And a route really refuses ───────────────────────────────────────────────

def test_a_telegram_signal_the_review_rejects_is_not_opened(fresh_db, ai):
    """End to end through scan_auto_execute.execute_auto_signal with a fake
    open_trade: the review's refusal becomes the skip reason and nothing is
    sent."""
    from tests.core.test_scan_messages_auto_execute_surface import _call
    _bus(fresh_db, "reversal_engine", "SELL", kind="engine")
    ai.reply = json.dumps({"execute": "SELL"})
    result, opened, _ = _call(rs={**ON, "max_open_trades": 3})
    assert opened == []
    assert "contradiction review" in (result["skip_reason"] or "")


def test_the_same_signal_with_the_review_off_is_opened(fresh_db, ai):
    from tests.core.test_scan_messages_auto_execute_surface import _call
    _bus(fresh_db, "reversal_engine", "SELL", kind="engine")
    ai.reply = json.dumps({"execute": "SELL"})
    result, opened, _ = _call(rs={**OFF, "max_open_trades": 3})
    assert len(opened) == 1 and ai.calls == 0
