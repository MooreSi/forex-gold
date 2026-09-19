"""The Breakout engine's panel, which was never ported.

`services/breakout_signal/panel_data.py` declares seventeen named operations
-- the whole NiceGUI Breakout page: its virtual balance, its drawdown, its
stats, its ML summary and metrics, and its performance split by session, ADX
band, breakout type and higher-timeframe bias.

`engines_controller` re-exported the module as `breakout` and **nothing ever
called it**. The Signal Generator tab showed the Reversal engine's ML gate and
virtual trades in detail and said nothing at all about the Breakout engine
beyond a Start/Stop card.

One endpoint, not nine. The panel reads them together on every refresh, and
nine endpoints is nine round-trips that can disagree about which moment they
describe -- the rule `/api/history/state` and `/api/engines/state` already
follow.

**Every read is individually guarded.** The engine keeps its own database and
a fresh install has none, so one missing table must not take the whole panel
down with it -- the same convention `services/dpm/performance.py` uses.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import engines as engines_router


@pytest.fixture
def panel(monkeypatch):
    state = {
        "stats": {"total": 12, "wins": 7, "win_rate": 58.3, "net_pnl": 210.4},
        "virtual_balance": 1210.4,
        "max_drawdown": 8.2,
        "ml_summary": {"trained": True, "labeled_count": 40, "min_needed": 15},
        "ml_metrics": {"brier_now": 0.21, "mcc_rolling": 0.3, "n_data": 40},
        "thresholds": {"min_train_samples": 15, "retrain_every": 5},
        "by_session": [{"session": "London", "n": 6, "net_pnl": 120.0}],
        "by_adx": [{"band": "20-25", "n": 4, "net_pnl": -30.0}],
        "by_type": [{"type": "range", "n": 5, "net_pnl": 80.0}],
        "by_bias": [{"bias": "bull", "n": 7, "net_pnl": 150.0}],
        "fail": set(),
    }

    def _reader(key, value):
        async def _read():
            if key in state["fail"]:
                raise RuntimeError(f"no {key} table on this install")
            return state[value]
        return _read

    for name, key in (("breakout_stats", "stats"),
                      ("breakout_virtual_balance", "virtual_balance"),
                      ("breakout_max_drawdown", "max_drawdown"),
                      ("breakout_ml_summary", "ml_summary"),
                      ("breakout_ml_metrics", "ml_metrics"),
                      ("breakout_perf_by_session", "by_session"),
                      ("breakout_perf_by_adx_band", "by_adx"),
                      ("breakout_perf_by_type", "by_type"),
                      ("breakout_perf_by_bias", "by_bias")):
        monkeypatch.setattr(engines_router.breakout_ctl, name, _reader(key, key))
    monkeypatch.setattr(engines_router.breakout_ctl, "breakout_ml_thresholds",
                        lambda: state["thresholds"])
    return state


class TestOneReadForTheWholePanel:

    def test_it_answers(self, make_client, panel):
        assert make_client().get("/api/engines/breakout/report").status_code == 200

    def test_it_carries_the_headline_numbers(self, make_client, panel):
        body = make_client().get("/api/engines/breakout/report").json()

        assert body["stats"]["win_rate"] == 58.3
        assert body["virtual_balance"] == 1210.4
        assert body["max_drawdown"] == 8.2

    def test_it_carries_the_ml_gate(self, make_client, panel):
        body = make_client().get("/api/engines/breakout/report").json()

        assert body["ml"]["summary"]["trained"] is True
        assert body["ml"]["metrics"]["brier_now"] == 0.21
        # The thresholds say what "trained" is measured against. Without them
        # "40 labelled" is a number with nothing to compare to.
        assert body["ml"]["thresholds"]["min_train_samples"] == 15

    def test_it_carries_every_performance_split(self, make_client, panel):
        body = make_client().get("/api/engines/breakout/report").json()

        assert body["by_session"][0]["session"] == "London"
        assert body["by_adx"][0]["band"] == "20-25"
        assert body["by_type"][0]["type"] == "range"
        assert body["by_bias"][0]["bias"] == "bull"


class TestAFreshInstallHasNoEngineDatabase:

    def test_one_missing_read_does_not_take_the_panel_down(self, make_client, panel):
        panel["fail"] = {"by_adx"}

        body = make_client().get("/api/engines/breakout/report").json()

        assert body["stats"]["win_rate"] == 58.3
        assert body["by_adx"] == []

    def test_a_missing_number_is_null_rather_than_zero(self, make_client, panel):
        # 0.0 drawdown reads as an engine that never lost. "Not known" does
        # not, and a fresh install is the second one.
        panel["fail"] = {"max_drawdown"}

        assert make_client().get("/api/engines/breakout/report").json()["max_drawdown"] is None

    def test_everything_failing_is_still_a_payload(self, make_client, panel):
        panel["fail"] = {"stats", "virtual_balance", "max_drawdown", "ml_summary",
                         "ml_metrics", "by_session", "by_adx", "by_type", "by_bias"}

        body = make_client().get("/api/engines/breakout/report").json()

        assert body["stats"] == {}
        assert body["by_session"] == []


class TestItPlacesNothing:

    def test_the_report_is_not_reachable_by_POST(self, make_client, panel):
        assert make_client().post("/api/engines/breakout/report").status_code == 405


def test_the_report_carries_the_edge_figures(make_client, panel, monkeypatch):
    """Profit factor and expectancy -- the NiceGUI Edge tab's numbers, which
    the React port had no counterpart for anywhere."""
    async def _edge():
        return {"profit_factor": 1.4, "expectancy": 3.2, "avg_win": 40.0,
                "avg_loss": 25.0, "closed": 127}

    monkeypatch.setattr(engines_router.breakout_ctl, "breakout_edge_stats", _edge)

    body = make_client().get("/api/engines/breakout/report").json()

    assert body["edge"]["profit_factor"] == 1.4
    assert body["edge"]["expectancy"] == 3.2


def test_a_missing_edge_read_does_not_take_the_panel_down(make_client, panel, monkeypatch):
    async def _boom():
        raise RuntimeError("no bo_signals table on this install")

    monkeypatch.setattr(engines_router.breakout_ctl, "breakout_edge_stats", _boom)

    body = make_client().get("/api/engines/breakout/report").json()

    assert body["edge"] == {}
    assert body["stats"]["win_rate"] == 58.3


class TestTheEngineSaysWhatItIsDoing:
    """The three reads that explain the numbers above them.

    `all_signals`, `analysis_log` and `adaptive_params` were the last of the
    seventeen `panel_data` operations with no caller. The log is the one that
    matters most: every other panel says WHAT the engine did, and this is the
    only thing in the app that says why it did not.
    """

    @pytest.fixture(autouse=True)
    def _reads(self, monkeypatch, panel):
        panel["signals"] = [{"id": 128, "signal_ref": "BO-324FEF14",
                             "direction": "BUY", "outcome": "loss"}]
        panel["log"] = [{"id": 82161, "ts": 1789848014.0, "result": "closed",
                         "suppressed_reason": "Market closed (weekend)"}]
        panel["params"] = {"min_adx_go": {"value": 30.0, "default": 28.0,
                                          "min": 20.0, "max": 45.0, "desc": "..."}}
        panel["asked"] = {}

        async def _signals(limit):
            panel["asked"]["signals"] = limit
            if "signals" in panel["fail"]:
                raise RuntimeError("no table")
            return panel["signals"]

        async def _log(limit):
            panel["asked"]["log"] = limit
            if "log" in panel["fail"]:
                raise RuntimeError("no table")
            return panel["log"]

        async def _params():
            if "params" in panel["fail"]:
                raise RuntimeError("no table")
            return panel["params"]

        monkeypatch.setattr(engines_router.breakout_ctl, "breakout_recent_signals", _signals)
        monkeypatch.setattr(engines_router.breakout_ctl, "breakout_analysis_log", _log)
        monkeypatch.setattr(engines_router.breakout_ctl, "breakout_adaptive_params", _params)

    def test_the_report_carries_them(self, make_client, panel):
        body = make_client().get("/api/engines/breakout/report").json()

        assert body["signals"][0]["signal_ref"] == "BO-324FEF14"
        assert body["log"][0]["suppressed_reason"] == "Market closed (weekend)"
        assert body["params"]["min_adx_go"]["value"] == 30.0

    def test_the_lists_are_bounded_by_the_server(self, make_client, panel):
        # Both grow without limit, and an unbounded read is the browser
        # deciding how expensive a request is.
        make_client().get("/api/engines/breakout/report")

        assert panel["asked"]["signals"] == engines_router.BREAKOUT_LIST_LIMIT
        assert panel["asked"]["log"] == engines_router.BREAKOUT_LIST_LIMIT

    def test_a_missing_log_does_not_take_the_panel_down(self, make_client, panel):
        panel["fail"] = {"log"}

        body = make_client().get("/api/engines/breakout/report").json()

        assert body["log"] == []
        assert body["signals"][0]["signal_ref"] == "BO-324FEF14"

    def test_missing_parameters_are_an_empty_object(self, make_client, panel):
        panel["fail"] = {"params"}

        assert make_client().get("/api/engines/breakout/report").json()["params"] == {}
