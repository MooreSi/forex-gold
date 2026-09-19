"""The London opening-range breakout report, and the one press that trades it.

Restored 2026-09-18 — the NiceGUI Trading page had this card and the React port
dropped it, so the report, its chart, the Execute button, the lot size and the
unattended auto-execute were all unreachable.

**`/execute` is the only endpoint here that places anything**, and no test in
this file lets it: the engine is the suite's sentinel and the routing is a
recorder. What is asserted is which node would receive the order and with what.

The property worth more than the routing: **the stop and target come from the
request, not from a fresh report.** The report moves as price does, so
recomputing inside the handler would open a trade against numbers that were
never on the operator's screen.
"""
from __future__ import annotations

import base64

import pytest

from backend.src.api.routers import orb as orb_router


def _report(**over) -> dict:
    row = {
        "direction": "bullish", "phase": "confirmed", "current_price": 2401.5,
        "asia_low": 2380.0, "asia_high": 2395.0, "asia_range": 15.0,
        "or_low": 2396.0, "or_high": 2400.0, "or_range": 4.0,
        "stop": 2398.0, "target": 2405.0, "target2": 2408.0, "rr": 2.0,
        "position_note": "above both ranges",
    }
    row.update(over)
    return row


@pytest.fixture
def orb(monkeypatch):
    state = {
        "report": _report(),
        "chart": b"PNGBYTES",
        "settings": {"orb_lot_size": 0.0, "orb_auto_execute_enabled": 0},
        "writes": [],
        "orders": [],
        "target": "local",
        "report_raises": None,
        "chart_raises": None,
        "order_error": None,
    }

    async def _build():
        if state["report_raises"]:
            raise state["report_raises"]
        return state["report"]

    def _chart(report):
        if state["chart_raises"]:
            raise state["chart_raises"]
        return state["chart"]

    async def _place(engine, **order):
        if state["order_error"]:
            raise state["order_error"]
        state["orders"].append(order)
        return {"mt5_ticket": 7, "entry_price": 2401.0, "where": state["target"]}

    monkeypatch.setattr(orb_router.notify_ctl, "build_orb_report", _build)
    monkeypatch.setattr(orb_router.notify_ctl, "build_orb_chart_image", _chart)
    monkeypatch.setattr(orb_router.trading_ctl, "get_risk_settings",
                        lambda: dict(state["settings"]))
    monkeypatch.setattr(orb_router.trading_ctl, "update_risk_settings",
                        lambda v: (state["writes"].append(v),
                                   state["settings"].update(v)))
    monkeypatch.setattr(orb_router.engines_ctl, "place_market_order", _place)
    monkeypatch.setattr(orb_router.engines_ctl, "control_target",
                        lambda: state["target"])
    return state


def _message(res) -> str:
    return res.json()["error"]["message"]


class TestReading:
    def test_the_report_and_its_chart_arrive_together(self, make_client, orb):
        body = make_client().get("/api/trading/orb").json()

        assert body["report"]["direction"] == "bullish"
        assert base64.b64decode(body["chart_png_base64"]) == b"PNGBYTES"

    def test_no_report_yet_is_a_state_not_a_failure(self, make_client, orb):
        """Before London opens there is nothing to show, and the tab says so.
        An empty card would read as a broken feature."""
        orb["report"] = None

        body = make_client().get("/api/trading/orb").json()

        assert body["report"] is None
        assert body["chart_png_base64"] is None

    def test_a_chart_that_will_not_render_does_not_take_the_report_down(
        self, make_client, orb,
    ):
        """The numbers are the point; the picture is not."""
        orb["chart_raises"] = RuntimeError("matplotlib backend missing")

        body = make_client().get("/api/trading/orb").json()

        assert body["report"]["direction"] == "bullish"
        assert body["chart_png_base64"] is None

    def test_a_report_that_cannot_be_built_says_why(self, make_client, orb):
        orb["report_raises"] = RuntimeError("no candles")

        res = make_client().get("/api/trading/orb")

        assert res.status_code == 409
        assert "no candles" in _message(res)

    def test_it_carries_the_two_settings_and_where_an_order_would_land(
        self, make_client, orb,
    ):
        orb["settings"] = {"orb_lot_size": 0.07, "orb_auto_execute_enabled": 1}
        orb["target"] = "remote"

        body = make_client().get("/api/trading/orb").json()

        assert body["lot_size"] == pytest.approx(0.07)
        assert body["auto_execute"] is True
        assert body["control_target"] == "remote"


class TestTheSettings:
    def test_the_lot_size_is_stored(self, make_client, orb):
        make_client().put("/api/trading/orb/settings", json={"lot_size": 0.05})

        assert orb["writes"] == [{"orb_lot_size": 0.05}]

    def test_zero_is_allowed_and_means_size_it_from_risk(self, make_client, orb):
        body = make_client().put("/api/trading/orb/settings",
                                 json={"lot_size": 0}).json()

        assert orb["writes"] == [{"orb_lot_size": 0.0}]
        assert body["lot_size"] == 0.0

    def test_a_negative_lot_size_is_refused(self, make_client, orb):
        res = make_client().put("/api/trading/orb/settings", json={"lot_size": -1})

        assert res.status_code == 400
        assert orb["writes"] == []

    def test_auto_execute_is_stored_as_the_0_or_1_the_column_holds(
        self, make_client, orb,
    ):
        make_client().put("/api/trading/orb/settings", json={"auto_execute": True})

        assert orb["writes"] == [{"orb_auto_execute_enabled": 1}]

    def test_an_empty_body_is_refused_rather_than_writing_nothing_quietly(
        self, make_client, orb,
    ):
        res = make_client().put("/api/trading/orb/settings", json={})

        assert res.status_code == 409
        assert orb["writes"] == []


class TestExecuting:
    def test_it_places_what_the_operator_was_shown(self, make_client, orb):
        """The stop and target come from the REQUEST. The report moves as price
        does, so recomputing here would trade numbers that were never on
        screen."""
        make_client().post("/api/trading/orb/execute", json={
            "direction": "BUY", "stop_loss": 2398.0, "take_profit": 2405.0,
        })

        assert orb["orders"] == [{
            "direction": "BUY", "stop_loss": 2398.0, "take_profit": 2405.0,
            "lot_size": None, "strategy": orb_router.STRATEGY,
            "source_name": orb_router.SOURCE_NAME,
        }]

    def test_it_is_tagged_so_these_trades_can_be_told_apart_later(
        self, make_client, orb,
    ):
        """A plain manual market order and an ORB execution are different
        things, and the history has to be able to say which."""
        make_client().post("/api/trading/orb/execute", json={
            "direction": "SELL", "stop_loss": 2410.0, "take_profit": 2380.0,
        })

        assert orb["orders"][0]["strategy"] == "orb_fixed"
        assert orb["orders"][0]["source_name"] == "ORB/IVB Report"

    def test_a_stored_lot_size_is_used(self, make_client, orb):
        orb["settings"]["orb_lot_size"] = 0.07

        make_client().post("/api/trading/orb/execute", json={
            "direction": "BUY", "stop_loss": 1.0, "take_profit": 2.0,
        })

        assert orb["orders"][0]["lot_size"] == pytest.approx(0.07)

    def test_zero_becomes_None_so_the_engine_sizes_it(self, make_client, orb):
        """`lot_size=None` is what tells the engine to size from the risk
        percentage. Sending 0 would be an order for no lots."""
        orb["settings"]["orb_lot_size"] = 0.0

        make_client().post("/api/trading/orb/execute", json={
            "direction": "BUY", "stop_loss": 1.0, "take_profit": 2.0,
        })

        assert orb["orders"][0]["lot_size"] is None

    def test_a_direction_the_engine_would_not_accept_never_reaches_it(
        self, make_client, orb,
    ):
        res = make_client().post("/api/trading/orb/execute", json={
            "direction": "sideways", "stop_loss": 1.0, "take_profit": 2.0,
        })

        assert res.status_code == 400
        assert orb["orders"] == []

    def test_a_refusal_reaches_the_operator_verbatim(self, make_client, orb):
        orb["order_error"] = ValueError(
            "DPM is disabled and no stop loss was given.")

        res = make_client().post("/api/trading/orb/execute", json={
            "direction": "BUY", "stop_loss": 1.0, "take_profit": 2.0,
        })

        assert res.status_code == 409
        assert "DPM is disabled" in _message(res)

    def test_an_unreachable_peer_is_a_refusal_not_a_silent_success(
        self, make_client, orb,
    ):
        orb["order_error"] = orb_router.engines_ctl.RemoteControlFailed(
            "The remote node could not be reached. Nothing was placed.")

        res = make_client().post("/api/trading/orb/execute", json={
            "direction": "BUY", "stop_loss": 1.0, "take_profit": 2.0,
        })

        assert res.status_code == 409
        assert "Nothing was placed" in _message(res)


class TestNothingElseTrades:
    def test_reading_the_report_places_nothing(self, make_client, orb):
        make_client().get("/api/trading/orb")

        assert orb["orders"] == []

    def test_saving_a_setting_places_nothing(self, make_client, orb):
        make_client().put("/api/trading/orb/settings", json={"lot_size": 0.05})

        assert orb["orders"] == []

    def test_executing_is_not_reachable_by_a_get(self, make_client, orb):
        """A GET that opens a position is one a browser prefetch can fire."""
        assert make_client().get("/api/trading/orb/execute").status_code == 405
        assert orb["orders"] == []
