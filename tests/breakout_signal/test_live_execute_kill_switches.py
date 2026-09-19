"""The six kill switches in front of a real Breakout order.

`breakout_signal_live_execute.py` was 22% covered. It is the one path in
`breakout_signal` that can place an actual MT5 order: `_execute_live` ends
in `open_trade_from_signal`, and everything before it exists to decide
whether that call happens.

**No test here reaches a broker.** The main engine is a recorder, the repo
is a recorder, and every gate is a stand-in. The property each refusal test
asserts is not "it returned early" but `main.opened == []` -- no order was
placed -- together with the status string written against the signal, since
a silent skip and a recorded skip look identical from outside and only one
of them can be diagnosed later.

`test_the_refresh_block_really_runs` is the guard rail on the guard rails.
The fill-time re-evaluation is wrapped in a bare `except`, so a stand-in
that raises would send the whole block -- including the momentum re-check
-- down the fallback path, and a momentum refusal test would then pass for
a reason that has nothing to do with momentum. That exact failure is
recorded in the Reversal engine's equivalent file.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.breakout_signal import breakout_signal_live_execute as live

_NOW = 1_758_000_000.0


def _candles(n, base=4000.0, step=0.4):
    """Candles that trend up gently: enough for ATR/ADX/MACD to produce a
    number, not enough for the momentum check to call it exhausted."""
    out = []
    for i in range(n):
        o = base + i * step
        out.append({"ts": 1_757_000_000 + i * 300, "open": o, "high": o + 1.0,
                    "low": o - 1.0, "close": o + step, "tick_volume": 100})
    return out


class _Bridge:
    def __init__(self):
        self.asked = []

    async def get_candles(self, timeframe, count):
        self.asked.append(timeframe)
        return _candles(count)


class _Main:
    """The main engine, recording instead of trading."""

    def __init__(self):
        self.created = []
        self.opened = []

    def create_signal(self, **kw):
        self.created.append(kw)
        return {"signal_id": 7788}

    async def open_trade_from_signal(self, signal_id, tick=None):
        self.opened.append((signal_id, tick))
        return {"mt5_ticket": 55501, "entry_price": 4002.0}


class _Engine(live._LiveExecuteMixin):
    def __init__(self, main, bridge):
        self._main_engine = main
        self._bridge = bridge


_SIGNAL = {
    "id": 42, "signal_ref": "BO-0042", "direction": "BUY",
    "entry_mid": 4002.0, "stop_loss": 3990.0,
    "tp1": 4010.0, "tp2": 4018.0, "tp3": 4030.0,
    "lot_size": 0.10, "ml_prob": 0.8, "broken_level": 3998.0,
    "created_at": _NOW - 30,
}


@pytest.fixture
def lab(monkeypatch):
    """Every gate open, every collaborator a stand-in. A test closes one."""
    state = {
        "risk": {"bo_live_execution": 1, "hour_blocklist_enabled": 0,
                 "kelly_sizing_enabled": 0},
        "schedule": (True, ""),
        "exposure": (True, ""),
        "momentum": (True, ""),
        "momentum_calls": [],
        "grid_template": None,
        "has_batch": False,
        "predict": None,
        "recent_closed": [],
        "results": [],
        "blocked_hours": (12, 13, 14),
        "session_allowed": (True, "London"),
    }

    def _momentum(direction, candles, atr):
        state["momentum_calls"].append((direction, len(candles), atr))
        return state["momentum"]

    monkeypatch.setattr("backend.src.db.database.get_risk_settings",
                        lambda: dict(state["risk"]))
    monkeypatch.setattr("backend.src.db.database.is_session_allowed",
                        lambda: state["session_allowed"])
    monkeypatch.setattr("backend.src.services.risk.schedule.check_trading_schedule",
                        lambda **kw: state["schedule"])
    monkeypatch.setattr(
        "backend.src.services.positions.core_internal_exposure_guard"
        ".check_internal_exposure", lambda *a, **kw: state["exposure"])
    monkeypatch.setattr(
        "backend.src.services.positions.core_grid_template_dispatch"
        ".grid_template_for_source", lambda src: state["grid_template"])
    monkeypatch.setattr(live, "check_momentum_exhaustion", _momentum)
    monkeypatch.setattr("backend.src.utils.regime.BREAKOUT_BLOCKED_HOURS_UTC",
                        state["blocked_hours"])
    monkeypatch.setattr(live.bdb, "update_live_exec_result",
                        lambda sig_id, ticket, vsid, status:
                        state["results"].append((sig_id, ticket, vsid, status)))
    monkeypatch.setattr(live.bdb, "get_recent_closed_signals",
                        lambda limit=50: list(state["recent_closed"]))
    monkeypatch.setattr(live.bdb, "get_signal_by_id", lambda sid: dict(_SIGNAL))

    import backend.src.services.breakout_signal.ml_engine as bo_ml
    monkeypatch.setattr(bo_ml, "has_batch", lambda: state["has_batch"])
    monkeypatch.setattr(bo_ml, "extract_features", lambda sig: {"f": 1.0})
    monkeypatch.setattr(bo_ml, "predict", lambda feats: state["predict"])
    return state


def _run(state, sig=None):
    main, bridge = _Main(), _Bridge()
    engine = _Engine(main, bridge)
    asyncio.run(engine._execute_live(dict(sig or _SIGNAL), 4002.0, None))
    return main, engine


def _status(state):
    assert state["results"], "nothing was recorded against the signal"
    return state["results"][-1][3]


# ── Guard rails on the guard rails ───────────────────────────────────────────

def test_a_clear_path_places_exactly_one_order(lab):
    main, _ = _run(lab)

    assert len(main.opened) == 1
    assert main.opened[0][0] == 7788
    assert _status(lab) == "success"


def test_the_refresh_block_really_runs(lab):
    """If a stand-in raised, the bare `except` would swallow it and every
    momentum test below would pass without momentum being consulted."""
    _run(lab)

    assert lab["momentum_calls"], "the fill-time re-evaluation never ran"


def test_the_recorded_ticket_is_the_brokers_not_invented(lab):
    _run(lab)

    assert lab["results"][-1][1] == 55501
    assert lab["results"][-1][2] == 7788


# ── The six kill switches ────────────────────────────────────────────────────

def test_no_main_engine_places_nothing_and_says_why(lab):
    engine = _Engine(None, _Bridge())
    asyncio.run(engine._execute_live(dict(_SIGNAL), 4002.0, None))

    assert _status(lab) == "skipped:no_main_engine"


def test_live_execution_switched_off_places_nothing(lab):
    lab["risk"]["bo_live_execution"] = 0

    main, _ = _run(lab)

    assert main.opened == []
    assert _status(lab) == "skipped:live_off"


def test_the_trading_schedule_blocks_and_names_the_reason(lab):
    lab["schedule"] = (False, "window closed")

    main, _ = _run(lab)

    assert main.opened == []
    assert _status(lab) == "skipped:schedule:window closed"


def test_the_internal_exposure_guard_blocks(lab):
    lab["exposure"] = (False, "exposure: 3 BUY already open")

    main, _ = _run(lab)

    assert main.opened == []
    assert _status(lab) == "skipped:exposure: 3 BUY already open"


def test_a_blocked_hour_blocks_only_when_the_blocklist_is_on(lab, monkeypatch):
    from datetime import datetime as _dt, timezone as _tz

    class _Clock(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 9, 19, 13, 5, tzinfo=_tz.utc)

    monkeypatch.setattr("datetime.datetime", _Clock)
    lab["risk"]["hour_blocklist_enabled"] = 1

    main, _ = _run(lab)

    assert main.opened == []
    assert "hour_blocklist" in _status(lab)


def test_the_same_hour_trades_when_the_blocklist_is_off(lab, monkeypatch):
    from datetime import datetime as _dt, timezone as _tz

    class _Clock(_dt):
        @classmethod
        def now(cls, tz=None):
            return _dt(2026, 9, 19, 13, 5, tzinfo=_tz.utc)

    monkeypatch.setattr("datetime.datetime", _Clock)
    lab["risk"]["hour_blocklist_enabled"] = 0

    main, _ = _run(lab)

    assert len(main.opened) == 1


def test_momentum_exhaustion_blocks_a_stale_signal(lab):
    lab["momentum"] = (False, "already extended 2.4 ATR")

    main, _ = _run(lab)

    assert main.opened == []
    assert "momentum re-check" in _status(lab)
    assert "already extended 2.4 ATR" in _status(lab)


def test_the_ml_gate_refuses_a_negative_expected_R(lab):
    lab["has_batch"] = True
    lab["predict"] = -0.2

    main, _ = _run(lab)

    assert main.opened == []
    assert "ML gate" in _status(lab)


def test_the_ml_gate_is_inert_without_a_trained_batch(lab):
    """A negative score from an untrained model must not block: has_batch()
    is what says the number means anything."""
    lab["has_batch"] = False
    lab["predict"] = -0.2

    main, _ = _run(lab)

    assert len(main.opened) == 1


# ── What reaches the broker when it does ─────────────────────────────────────

def test_the_stop_and_targets_are_passed_through_unchanged(lab):
    main, _ = _run(lab)

    created = main.created[0]
    assert created["stop_loss"] == 3990.0
    assert (created["tp1"], created["tp2"], created["tp3"]) == (4010.0, 4018.0, 4030.0)
    assert created["direction"] == "BUY"
    assert created["source_name"] == "Breakout Engine"
    assert created["notes"] == "BO-0042"


def test_a_failure_from_the_broker_is_recorded_against_the_signal(lab):
    main, bridge = _Main(), _Bridge()

    async def _boom(signal_id, tick=None):
        raise RuntimeError("no prices for XAUUSD")

    main.open_trade_from_signal = _boom
    engine = _Engine(main, bridge)
    asyncio.run(engine._execute_live(dict(_SIGNAL), 4002.0, None))

    sig_id, ticket, vsid, status = lab["results"][-1]
    assert ticket is None
    assert vsid == 7788, "the created signal id must survive the failure"
    assert status.startswith("failed:")
    assert "no prices for XAUUSD" in status


# ── The entry band ───────────────────────────────────────────────────────────
# _grid_zone decides the (low, high) the broker is given. With a grid EA
# template assigned, the legs are staged ACROSS that band, so a band that is
# wrong is a set of orders at prices nobody chose.

class TestTheEntryBand:

    def test_without_a_grid_template_it_is_the_plain_half_point_band(self, lab):
        assert live._grid_zone(dict(_SIGNAL), "BUY", 4002.0) == (4001.5, 4002.5)

    def test_a_grid_template_widens_a_buy_to_the_retest(self, lab):
        """Level below, entry above: the band is the ground price broke
        through, not an arbitrary point either side of the market."""
        lab["grid_template"] = {"name": "Grid-A"}

        assert live._grid_zone(dict(_SIGNAL), "BUY", 4002.0) == (3998.0, 4002.0)

    def test_a_grid_template_widens_a_sell_the_other_way(self, lab):
        lab["grid_template"] = {"name": "Grid-A"}
        sig = dict(_SIGNAL, broken_level=4006.0)

        assert live._grid_zone(sig, "SELL", 4002.0) == (4002.0, 4006.0)

    def test_a_band_too_narrow_to_spread_legs_across_falls_back(self, lab):
        """Below one point the legs collapse onto the market, where the
        broker's own stops level rejects them."""
        lab["grid_template"] = {"name": "Grid-A"}
        sig = dict(_SIGNAL, broken_level=4001.5)

        assert live._grid_zone(sig, "BUY", 4002.0) == (4001.5, 4002.5)

    def test_a_missing_level_falls_back_rather_than_using_zero(self, lab):
        """A zero level would produce a band from $0 to the market."""
        lab["grid_template"] = {"name": "Grid-A"}
        sig = dict(_SIGNAL, broken_level=None)

        assert live._grid_zone(sig, "BUY", 4002.0) == (4001.5, 4002.5)

    def test_a_level_on_the_wrong_side_falls_back(self, lab):
        """A sweep whose level sits above a BUY entry inverts the band."""
        lab["grid_template"] = {"name": "Grid-A"}
        sig = dict(_SIGNAL, broken_level=4009.0)

        assert live._grid_zone(sig, "BUY", 4002.0) == (4001.5, 4002.5)


# ── Staging grid legs at signal creation ─────────────────────────────────────

class TestGridStagingOnArrival:

    def _stage(self, state):
        main, bridge = _Main(), _Bridge()
        engine = _Engine(main, bridge)
        staged = asyncio.run(engine._maybe_stage_grid_template(42, 4002.0, None))
        return staged, main

    def test_no_grid_template_means_no_early_staging(self, lab):
        staged, main = self._stage(lab)

        assert staged is False
        assert main.opened == []

    def test_a_grid_template_stages_the_legs_immediately(self, lab):
        lab["grid_template"] = {"name": "Grid-A"}

        staged, main = self._stage(lab)

        assert staged is True
        assert len(main.opened) == 1

    def test_staging_obeys_the_live_execution_switch(self, lab):
        lab["grid_template"] = {"name": "Grid-A"}
        lab["risk"]["bo_live_execution"] = 0

        staged, main = self._stage(lab)

        assert staged is False
        assert main.opened == []

    def test_staging_is_held_when_the_session_is_switched_off(self, lab):
        """The same Trading Markets check _check_outcomes applies before
        triggering -- early staging must not route around it."""
        lab["grid_template"] = {"name": "Grid-A"}
        lab["session_allowed"] = (False, "Asian")

        staged, main = self._stage(lab)

        assert staged is False
        assert main.opened == []

    def test_a_signal_already_sent_to_the_broker_is_not_sent_twice(self, lab,
                                                                  monkeypatch):
        """vantage_signal_id is what stops a second execution."""
        lab["grid_template"] = {"name": "Grid-A"}
        monkeypatch.setattr(live.bdb, "get_signal_by_id",
                            lambda sid: dict(_SIGNAL, vantage_signal_id=7788))

        staged, main = self._stage(lab)

        assert staged is False
        assert main.opened == []
