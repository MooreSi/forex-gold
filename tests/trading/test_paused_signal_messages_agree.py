"""While trading is paused, every Telegram signal alert says the same thing.

Owner, 2026-10-05: with the daily goal reached, some alerts said the signal
was queued ("Will auto-activate when price returns to zone") and others said
"Auto-execution failed: Trading paused until 22:00 -- MT5 order blocked: Daily
goal secured: ...". Same pause, two stories, and the first one was wrong: a
queued signal does not activate while trading is paused.

Both routes end in the same place -- the signal row stays `pending`, and the
pending watcher opens it only if trading resumes while it is still live -- so
both say so, in one wording, led by the same headline the header badge shows.

Nothing here reaches a broker: `open_trade` refuses on the pause before any
bridge call, and the bridge is a fake that only returns a tick.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.risk import pause_message as pm
from backend.src.services.trading import open_trade as cot
from backend.src.services.trading import scan_auto_execute as ae
from tests.core.test_scan_messages_auto_execute_surface import (
    _IN_ZONE_TICK, _OUT_OF_ZONE_TICK, _PARSED, _FakeBridge,
)

_REASON = "Daily goal secured: +$30.00 today vs a goal of $25.00"


@pytest.fixture
def paused(fresh_db):
    until = time.time() + 3600
    with db.db():
        db.set_app_config("trade_pause_until", str(until))
        db.set_app_config("risk_halt_reason", _REASON)
    return until


def _run(tick, tg_id):
    bridge = _FakeBridge(tick)

    async def _no_followup(*a):
        return False

    async def _balance():
        return 1000.0

    return asyncio.run(ae.execute_auto_signal(
        dict(_PARSED), tg_id, "Chan", "Chan", "scale_out", {}, True, False,
        "", "", bridge,
        get_open_trades_fn=lambda: [],
        find_and_apply_instant_followup_fn=_no_followup,
        check_pre_trade_filters_fn=lambda *a, **kw: None,
        suggest_lot_size_fn=lambda *a: 0.01,
        get_trading_balance_fn=_balance,
        open_trade_fn=lambda **kw: cot.open_trade(bridge, **kw),
    ))


def _statuses():
    with db.db() as conn:
        return [r[0] for r in conn.execute("SELECT status FROM vantage_signals")]


class TestOneStoryForOnePause:
    def test_in_zone_and_out_of_zone_say_the_same_thing(self, paused):
        in_zone = _run(_IN_ZONE_TICK, "tg-in")["skip_reason"]
        out_of_zone = _run(_OUT_OF_ZONE_TICK, "tg-out")["skip_reason"]

        assert in_zone == out_of_zone

    def test_it_is_the_pause_not_a_failure_or_a_promise(self, paused):
        text = _run(_IN_ZONE_TICK, "tg-in")["skip_reason"]

        assert text == pm.signal_queued(paused, _REASON)
        assert "failed" not in text.lower()
        assert "auto-activate when price returns" not in text

    def test_it_leads_with_the_headers_words(self, paused):
        text = _run(_OUT_OF_ZONE_TICK, "tg-out")["skip_reason"]

        assert text.startswith("⏸️ Goal Achieved Paused until ")
        assert _REASON in text

    def test_both_signals_are_still_queued_exactly_as_before(self, paused):
        """Wording only. Where the signal ends up does not change."""
        _run(_IN_ZONE_TICK, "tg-in")
        _run(_OUT_OF_ZONE_TICK, "tg-out")

        assert _statuses() == ["pending", "pending"]

    def test_nothing_was_executed(self, paused):
        assert _run(_IN_ZONE_TICK, "tg-in")["executed"] is False


class TestNotPaused:
    def test_an_out_of_zone_signal_keeps_its_own_queued_wording(self, fresh_db):
        """Negative control: the pause wording appears only under a pause."""
        text = _run(_OUT_OF_ZONE_TICK, "tg-out")["skip_reason"]

        assert text.startswith("Signal queued")
        assert "Paused" not in text


class TestTheWording:
    def test_a_loss_halt_reads_trading_paused(self):
        text = pm.signal_queued(time.time() + 60, "Daily loss limit hit: $-178.45")

        assert text.startswith("⏸️ Trading Paused until ")

    def test_no_resume_time_still_names_the_pause(self):
        assert pm.signal_queued(0.0, _REASON).startswith("⏸️ Goal Achieved Paused (")

    def test_the_brain_still_files_it_under_halts(self):
        from backend.src.services.brain import gates
        text = pm.signal_queued(time.time() + 60, _REASON)

        assert gates.classify(text, False) == ("blocked", "halt")


class TestTheOtherPausedRoutes:
    def test_auto_execute_off_names_the_same_pause(self, paused, monkeypatch):
        from backend.src.services.signals import scan_staleness
        monkeypatch.setattr(scan_staleness.db_module, "is_session_allowed",
                            lambda rs: (True, "london"))

        async def _is_paused():
            return True

        out = asyncio.run(scan_staleness.resolve_strategy_and_skip_reason(
            {},
            "Chan", "BUY GOLD", dict(_PARSED), False, None, None,
            is_trading_paused_fn=_is_paused,
        ))

        assert out["skip_reason"] == pm.signal_not_executed(paused, _REASON)

    def test_the_limit_runner_names_the_same_pause(self, paused):
        text = pm.limit_not_placed(paused, _REASON)

        assert text.startswith(pm.headline(paused, _REASON))
        assert pm.signal_queued(paused, _REASON).startswith(pm.headline(paused, _REASON))
