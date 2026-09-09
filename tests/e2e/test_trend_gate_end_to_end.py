"""The trend gate, driven end-to-end through the real pipeline.

reversal-engine/080 put the higher-timeframe bias gate on the **shared** open
path so that Telegram signals are governed by it too, not only the Reversal
Engine's own. The unit tests in `tests/core` pin `htf_bias_blocks` itself. This
file proves the gate is actually CONSULTED when a real signal arrives — the
wiring, not the rule.

Why that needs its own test: the gate has six call sites, and the failure it
exists to prevent was a whole day of counter-trend trades that each passed
their own filters. A gate that is correct and unreachable looks exactly like a
gate that is working, in a log that shows no refusals.

**Where the fake boundary is drawn.** `level_detector.get_htf_bias` is stubbed;
everything above it runs for real — the Telegram reader, the parser,
auto-execute, `open_trade`, and the broker boundary as `FakeMT5Bridge`. The
bias COMPUTATION has its own tests and depends on candle shapes that are
fragile to fake convincingly; what is untested elsewhere, and asserted here, is
that a bearish read reaches the decision to refuse a BUY.

**This file found reversal-engine/080's third uncovered route.** When it was
first written, the two "refused" tests below failed and all three controls
passed: a BUY opened at market against a bearish bias with the gate on. The
gate lives in `resolution.resolve_open_trade_params`, and
`scan_auto_execute` — a fresh Telegram signal whose price is already in its
zone — opens through `open_trade` directly and never calls it. That module's
own comment says so, about the *schedule* gate, which had the identical gap
patched for it on 2026-08-06. 080's audit table listed IME and limit orders
and missed this one.

NO REAL OR DEMO ORDER CAN BE PLACED HERE — same harness as
`test_killer_demos.py`: FakeMT5Bridge, no network, empty bridge URL.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.runtime import TradingRuntime
from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.telegram.fake_reader import FakeTelegramReader

BASE = 1_700_000_000.0

BUY_SIGNAL = (
    "Buy Gold 2399 - 2402\n"
    "Stop Loss 2392\n"
    "TP1 2410  TP2 2418  TP3 2426"
)

SELL_SIGNAL = (
    "Sell Gold 2399 - 2402\n"
    "Stop Loss 2409\n"
    "TP1 2391  TP2 2383  TP3 2375"
)

CONFIG = {
    "starting_balance": 1000.0, "anthropic_api_key": "",
    "mt5_bridge_url": "", "mt5_native_bridge_enabled": False,
    "telegram_api_id": "", "telegram_api_hash": "",
    "sessions_dir": "./data/test_sessions",
}


@pytest.fixture(autouse=True)
def _clear_bias_cache():
    """`current_htf_bias` caches for 60s in a module global. Without this the
    second test in a run reads the first one's answer."""
    from backend.src.services.risk import governor
    governor._htf_bias_cache["ts"] = 0.0
    yield
    governor._htf_bias_cache["ts"] = 0.0


@pytest.fixture
def bias(monkeypatch):
    """Set the higher-timeframe read the whole app will see."""
    def _set(value: str):
        from backend.src.services.reversal_engine import level_detector
        monkeypatch.setattr(level_detector, "get_htf_bias",
                            lambda *a, **k: value)
        from backend.src.services.risk import governor
        governor._htf_bias_cache["ts"] = 0.0
        return value
    return _set


def _runtime(clock: dict, text: str = BUY_SIGNAL):
    engine = TradingRuntime(CONFIG)
    engine._bridge = FakeMT5Bridge(
        seed=1, scenario={"anchors": [[0, 2400.5], [300, 2401.0]]},
        base_ts=BASE, clock=lambda: clock["now"], starting_balance=1000.0,
    )
    engine.set_telegram_reader(
        reader := FakeTelegramReader(CONFIG, scenario={
            "signals": [{"at": 0, "channel": "Debug Channel", "text": text}]})
    )
    return engine, reader


def _run_one_signal(engine, reader):
    reader.feed_due(now=1.0)
    return asyncio.run(engine._scan_messages())


def _open_rows(fresh_db) -> int:
    with fresh_db.db() as conn:
        return conn.execute(
            "SELECT COUNT(*) c FROM vantage_simulated_trades WHERE status='open'"
        ).fetchone()["c"]


class TestABuyAgainstABearishBiasIsRefused:
    def test_no_position_is_opened(self, fresh_db, bias):
        bias("bearish")
        clock = {"now": BASE}
        engine, reader = _runtime(clock)
        fresh_db.update_risk_settings({
            "auto_execute_signals": 1, "accept_tg_signals": 1,
            "htf_bias_gate_enabled": 1,
        })

        _run_one_signal(engine, reader)

        assert asyncio.run(engine._bridge.get_positions()) == [], (
            "a BUY was opened against a bearish higher-timeframe bias"
        )
        assert _open_rows(fresh_db) == 0

    def test_the_signal_is_still_recorded(self, fresh_db, bias):
        """Refused is not the same as unseen. The signal must still be on
        record, or a refusal is indistinguishable from a parser failure."""
        bias("bearish")
        clock = {"now": BASE}
        engine, reader = _runtime(clock)
        fresh_db.update_risk_settings({
            "auto_execute_signals": 1, "accept_tg_signals": 1,
            "htf_bias_gate_enabled": 1,
        })

        signals = _run_one_signal(engine, reader)

        assert signals, "the signal was not recorded at all"
        assert all(not s.get("auto_executed") for s in signals)


class TestTheControls:
    """Without these, a gate that refused EVERYTHING would pass the tests
    above — which is the failure mode the runbook calls OVER-refusal."""

    def test_the_same_buy_trades_when_the_bias_agrees(self, fresh_db, bias):
        bias("bullish")
        clock = {"now": BASE}
        engine, reader = _runtime(clock)
        fresh_db.update_risk_settings({
            "auto_execute_signals": 1, "accept_tg_signals": 1,
            "htf_bias_gate_enabled": 1,
        })

        _run_one_signal(engine, reader)

        assert _open_rows(fresh_db) == 1, (
            "the gate refused a BUY that runs WITH a bullish bias"
        )

    def test_and_when_the_gate_is_OFF_the_bias_is_ignored(self, fresh_db, bias):
        """It ships off. An install that has not turned it on must behave
        exactly as it did before the gate existed."""
        bias("bearish")
        clock = {"now": BASE}
        engine, reader = _runtime(clock)
        fresh_db.update_risk_settings({
            "auto_execute_signals": 1, "accept_tg_signals": 1,
            "htf_bias_gate_enabled": 0,
        })

        _run_one_signal(engine, reader)

        assert _open_rows(fresh_db) == 1, (
            "a counter-bias BUY was refused with the gate switched OFF"
        )

    def test_an_undecided_bias_does_not_block(self, fresh_db, bias):
        """`current_htf_bias` returns "" when it cannot tell, and a filter that
        refused trades on a feed hiccup would stop all trading."""
        bias("")
        clock = {"now": BASE}
        engine, reader = _runtime(clock)
        fresh_db.update_risk_settings({
            "auto_execute_signals": 1, "accept_tg_signals": 1,
            "htf_bias_gate_enabled": 1,
        })

        _run_one_signal(engine, reader)

        assert _open_rows(fresh_db) == 1


class TestTheOtherDirection:
    """Without a SELL anywhere in this file, hardcoding the direction to "BUY"
    inside the gate changes nothing and the mutation survives. It did."""

    def _drive(self, fresh_db, bias_value, bias, text):
        bias(bias_value)
        clock = {"now": BASE}
        engine, reader = _runtime(clock, text)
        fresh_db.update_risk_settings({
            "auto_execute_signals": 1, "accept_tg_signals": 1,
            "htf_bias_gate_enabled": 1,
        })
        _run_one_signal(engine, reader)
        return _open_rows(fresh_db)

    def test_a_sell_against_a_bullish_bias_is_refused(self, fresh_db, bias):
        assert self._drive(fresh_db, "bullish", bias, SELL_SIGNAL) == 0, (
            "a SELL was opened against a bullish higher-timeframe bias"
        )

    def test_a_sell_WITH_a_bearish_bias_trades(self, fresh_db, bias):
        """The control for the case above, and the one that makes a hardcoded
        direction detectable."""
        assert self._drive(fresh_db, "bearish", bias, SELL_SIGNAL) == 1, (
            "the gate refused a SELL that runs WITH a bearish bias"
        )
