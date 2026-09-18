"""The Backtest tab's one expensive endpoint.

Nothing here places an order — a backtest walks data the broker has already
published — but two things can mislead an owner reading the results, and both
are asserted:

1. **A strategy the walk refused must arrive as a refusal, not as zeros.**
   `unsupported_reason` exists because "0 trades, 0 loss" sitting beside a row
   with a real drawdown reads as an argument FOR the strategy that was never
   simulated.
2. **The filter report must survive.** A backtest over three signals is not a
   backtest, and without the report the only visible symptom is a small trade
   count that looks like a quiet month.
"""
from __future__ import annotations

import dataclasses

import pytest

from backend.src.api.routers import backtest as bt_router


@dataclasses.dataclass
class _Stats:
    strategy: str
    trades: int = 12
    wins: int = 7
    losses: int = 5
    win_rate: float = 58.3
    total_pnl: float = 145.20
    total_commission: float = 8.40
    avg_win: float = 42.0
    avg_loss: float = -21.0
    profit_factor: float = 1.9
    max_drawdown_pct: float = 6.2
    sharpe: float = 0.8
    final_balance: float = 1145.20
    equity_curve: list = dataclasses.field(default_factory=lambda: [1000.0, 1145.2])
    unsupported_reason: str = ""


@dataclasses.dataclass
class _Filter:
    total: int = 40
    valid: int = 30
    out_of_window: int = 8
    zero_sl: int = 1
    point_entry: int = 1
    wide_sl: int = 0
    bad_tp: int = 0
    candle_start: str = "2026-05-16 00:00 UTC"
    candle_end: str = "2026-06-15 00:00 UTC"
    signal_start: str = "2026-05-01 00:00 UTC"
    signal_end: str = "2026-06-15 00:00 UTC"


@pytest.fixture
def lab(monkeypatch, sentinel_engine):
    """The backtest engine, scripted. No candles are fetched from a broker."""
    state = {
        "signals": ["s1", "s2"],
        "kept": ["s1"],
        "filter": _Filter(),
        "results": {"scale_out": _Stats("scale_out")},
        "candles": [{"ts": 1.0}] * 288,
        "calls": [],
    }

    async def _candles(symbol, timeframe, count):
        state["calls"].append(("get_candles_for_symbol", symbol, timeframe, count))
        return state["candles"]

    sentinel_engine.get_candles_for_symbol = _candles
    monkeypatch.setattr(bt_router.bt_ctl, "signals_from_db",
                        lambda live_only: state["signals"])
    monkeypatch.setattr(bt_router.bt_ctl, "filter_signals",
                        lambda sigs, candles, max_sl: (state["kept"], state["filter"]))
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest",
                        lambda *a, **k: state["results"])
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest_ticks",
                        lambda *a, **k: state["results"])
    return state


def _run(client, **over):
    body = {"strategies": ["scale_out"], "timeframe": "M5", "days": 30}
    body.update(over)
    return client.post("/api/backtest/run", json=body)


# ── The options the form builds itself from ──────────────────────────────────

def test_the_form_is_offered_the_strategies_and_the_backtestable_templates(
    make_client, monkeypatch,
):
    monkeypatch.setattr(bt_router.trading_ctl, "build_strategy_catalogue",
                        lambda: [{"key": "scale_out", "name": "Scale Out"}])
    monkeypatch.setattr(bt_router.broker_ctl, "list_ea_templates",
                        lambda: [{"name": "Grid Runner"}])
    monkeypatch.setattr(bt_router.bt_ctl, "summarise_templates",
                        lambda t: [{"name": "Grid Runner", "supported": False,
                                    "reason": "grid legs are not simulated"}])

    body = make_client().get("/api/backtest/options").json()

    assert body["strategies"] == [{"key": "scale_out", "name": "Scale Out"}]
    assert body["templates"][0]["reason"] == "grid legs are not simulated"
    assert body["timeframes"][0] == "M1"
    assert body["granularities"] == ["candles", "ticks"]


def test_the_options_carry_the_minimum_trades_a_split_side_needs(
    make_client, monkeypatch,
):
    """docs/simon-handover/036 — the page shows it, because a split that
    reports a number below it is reporting noise."""
    monkeypatch.setattr(bt_router.trading_ctl, "build_strategy_catalogue", lambda: [])
    monkeypatch.setattr(bt_router.broker_ctl, "list_ea_templates", lambda: [])
    monkeypatch.setattr(bt_router.bt_ctl, "summarise_templates", lambda t: [])
    monkeypatch.setattr(bt_router.bt_ctl, "MIN_TRADES_PER_SIDE", 30)

    assert make_client().get("/api/backtest/options").json()["min_trades_per_side"] == 30


# ── Running a walk ───────────────────────────────────────────────────────────

def test_a_run_returns_one_row_per_strategy_with_its_numbers(make_client, lab):
    body = _run(make_client()).json()

    assert len(body["results"]) == 1
    row = body["results"][0]
    assert row["strategy"] == "scale_out"
    assert row["trades"] == 12
    assert row["total_pnl"] == 145.20
    assert row["equity_curve"] == [1000.0, 1145.2]


def test_a_refused_strategy_carries_its_reason_and_is_not_just_zeros(make_client, lab):
    """The assertion this whole endpoint is shaped around."""
    lab["results"] = {
        "template:Grid Runner": _Stats(
            "template:Grid Runner", trades=0, wins=0, losses=0, win_rate=0.0,
            total_pnl=0.0, profit_factor=0.0, max_drawdown_pct=0.0,
            final_balance=1000.0, equity_curve=[],
            unsupported_reason="grid legs are not simulated on candles",
        ),
    }

    row = make_client().post("/api/backtest/run", json={
        "strategies": ["template:Grid Runner"]}).json()["results"][0]

    assert row["trades"] == 0
    assert row["unsupported_reason"] == "grid legs are not simulated on candles"


def test_the_filter_report_reaches_the_page(make_client, lab):
    body = _run(make_client()).json()

    assert body["filtered"]["total"] == 40
    assert body["filtered"]["out_of_window"] == 8
    assert body["filtered"]["candle_start"] == "2026-05-16 00:00 UTC"
    assert body["signals_loaded"] == 2
    assert body["candles_loaded"] == 288


def test_the_candle_window_is_asked_for_in_bars_not_days(make_client, lab):
    """The bridge counts bars. 30 days of M5 is 8,640 of them, and asking for
    30 would walk half an hour of history and report it as a month."""
    _run(make_client(), timeframe="M5", days=30)

    assert lab["calls"] == [("get_candles_for_symbol", "XAUUSD", "M5", 288 * 30)]


def test_a_different_timeframe_changes_the_bar_count(make_client, lab):
    """Negative control for the test above: a fixed number would pass it."""
    _run(make_client(), timeframe="H1", days=10)

    assert lab["calls"] == [("get_candles_for_symbol", "XAUUSD", "H1", 24 * 10)]


def test_the_tick_walk_is_used_when_ticks_are_asked_for(
    make_client, lab, sentinel_engine, monkeypatch,
):
    async def _ticks(from_ts, to_ts):
        return [{"time": 1_750_000_000.0, "bid": 2431.0, "ask": 2431.3}]

    sentinel_engine.get_ticks_range = _ticks
    seen = []
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest",
                        lambda *a, **k: seen.append("candles") or lab["results"])
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest_ticks",
                        lambda *a, **k: seen.append("ticks") or lab["results"])

    _run(make_client(), granularity="ticks")

    assert seen == ["ticks"]


def test_the_candle_walk_is_the_default(make_client, lab, monkeypatch):
    seen = []
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest",
                        lambda *a, **k: seen.append("candles") or lab["results"])
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest_ticks",
                        lambda *a, **k: seen.append("ticks") or lab["results"])

    _run(make_client())

    assert seen == ["candles"]


def test_the_cost_assumptions_are_forwarded_not_defaulted_here(make_client, lab, monkeypatch):
    """Spread and commission decide whether a strategy looks profitable. If
    this layer substituted its own, the page would be reporting a backtest
    nobody configured."""
    seen = {}
    monkeypatch.setattr(bt_router.bt_ctl, "run_backtest",
                        lambda *a, **k: seen.update(k) or lab["results"])

    _run(make_client(), spread_pts=1.25, commission_per_lot=3.5,
         starting_balance=5000.0, risk_pct=0.5, lots_per_trade=0.02,
         split_fraction=0.3)

    assert seen["spread_pts"] == 1.25
    assert seen["commission_per_lot"] == 3.5
    assert seen["starting_balance"] == 5000.0
    assert seen["risk_pct"] == 0.5
    assert seen["lots_per_trade"] == 0.02
    assert seen["split_fraction"] == 0.3


# ── When there is nothing to walk ────────────────────────────────────────────

def test_no_recorded_signals_says_so_rather_than_reporting_an_empty_backtest(
    make_client, lab,
):
    """Zero trades from zero signals and zero trades from a bad filter look
    identical on screen, and mean completely different things."""
    lab["signals"] = []

    body = _run(make_client()).json()

    assert body["results"] == []
    assert "No recorded signals" in body["note"]


def test_no_candles_is_refused_with_something_to_act_on(make_client, lab):
    lab["candles"] = []

    r = _run(make_client())

    assert r.status_code == 409
    assert "bridge is connected" in r.json()["error"]["message"]


def test_an_unknown_timeframe_never_reaches_the_bridge(make_client, lab):
    r = _run(make_client(), timeframe="7y")

    assert r.status_code == 400
    assert lab["calls"] == []


def test_a_run_with_no_strategies_is_rejected(make_client, lab):
    r = make_client().post("/api/backtest/run", json={"strategies": []})

    assert r.status_code == 422
    assert lab["calls"] == []


def test_the_run_endpoint_is_not_reachable_by_GET(make_client, lab):
    assert make_client().get("/api/backtest/run").status_code == 405


# ── The tick walk gets ticks ─────────────────────────────────────────────────

class TestTheTickWalk:
    """Handing the tick walk candles produces a full set of numbers computed
    from the wrong data — the worst kind of wrong here, because nothing about
    the result looks unusual. The first version of this router did exactly
    that; these are the tests that would have caught it."""

    @pytest.fixture
    def ticks(self, lab, sentinel_engine):
        rows = [{"time": 1_750_000_000.0 + i, "bid": 2431.0, "ask": 2431.3}
                for i in range(500)]
        lab["ticks"] = rows

        async def _ticks(from_ts, to_ts):
            lab["calls"].append(("get_ticks_range", from_ts, to_ts))
            return lab["ticks"]

        sentinel_engine.get_ticks_range = _ticks
        return lab

    def test_the_walk_is_handed_ticks_not_candles(self, make_client, ticks, monkeypatch):
        seen = {}
        monkeypatch.setattr(bt_router.bt_ctl, "run_backtest_ticks",
                            lambda kept, data, strategies, **k:
                                seen.update(rows=data) or ticks["results"])

        _run(make_client(), granularity="ticks")

        assert seen["rows"] is ticks["ticks"]
        assert seen["rows"] is not ticks["candles"]

    def test_it_asks_the_bridge_for_a_time_range_bounded_to_one_day(
        self, make_client, ticks,
    ):
        """Tick history is tens of megabytes a day and the bridge bounds it.
        Asking for a month gets nothing back."""
        _run(make_client(), granularity="ticks", days=30)

        call = next(c for c in ticks["calls"] if c[0] == "get_ticks_range")
        assert round(call[2] - call[1]) == bt_router.TICKS_MAX_DAYS * 86_400

    def test_the_response_says_how_many_ticks_and_over_what(self, make_client, ticks):
        """A tick walk over one day reported beside a "30 days" form field would
        read as a month of evidence."""
        body = _run(make_client(), granularity="ticks", days=30).json()

        assert body["candles_loaded"] == 500
        assert "500 ticks" in body["note"]
        assert "one day per request" in body["note"]

    def test_no_ticks_is_refused_with_something_to_act_on(self, make_client, ticks):
        ticks["ticks"] = []

        r = _run(make_client(), granularity="ticks")

        assert r.status_code == 409
        assert "one day per request" in r.json()["error"]["message"]

    def test_the_candle_walk_still_gets_candles(self, make_client, lab, monkeypatch):
        """Negative control for the first test in this class."""
        seen = {}
        monkeypatch.setattr(bt_router.bt_ctl, "run_backtest",
                            lambda kept, data, strategies, **k:
                                seen.update(rows=data) or lab["results"])

        _run(make_client())

        assert seen["rows"] is lab["candles"]
