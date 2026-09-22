"""Set & Forget — the Alex G swing method as a section of the Trading tab.

The property this file exists to hold is the first class below: **no endpoint
in this module can place, close or modify anything.** The section has an
Execute button, and the obvious way to build one is a `/setforget/execute`
endpoint that opens the trade. That would be a second order path in an app that
has exactly one, and the two would drift — which is why the button posts to
`api/routers/orders.py` instead and why this file asserts, rather than assumes,
that nothing here reaches the engine's money methods.

After that: the free read and the billable one return the SAME shape, so the
page has one renderer rather than two that can disagree about how a setup
looks; and a chart that cannot be read is a refusal with the reason in it, not
an empty card.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import setforget as sf_router

_MONEY_CALLS = ("open_manual_market_order", "open_manual_limit_order",
                "close_trade", "partial_close_trade", "open_trade_from_signal")


def _candidate(**over) -> dict:
    row = {
        "direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
        "take_profit": 2040.0, "order_type": "limit",
        "risk": 13.0, "reward": 55.0, "rr": 4.23,
        "distance": 15.0, "distance_days": 0.75,
        "stage": "triggered",
        "trigger": {"kind": "shift_of_structure", "ts": 1.0, "level": 1972.0},
        "zone": {"kind": "demand", "low": 1975.0, "high": 1985.0, "touches": 2},
        "target_zone": {"kind": "supply", "low": 2040.0, "high": 2050.0,
                        "touches": 1},
    }
    row.update(over)
    return row


@pytest.fixture
def sf(monkeypatch):
    state = {
        "evidence": {
            "price": 2000.0, "weekly_bias": "bullish", "daily_bias": "bullish",
            "entry_bias": "bullish", "entry_timeframe": "4H", "zones": [],
            "atr": 6.0, "daily_atr": 20.0,
            "ema_fast": 1995.0, "ema_slow": 1960.0, "rsi": 52.0,
            "confirmation": None, "impulse": None, "fib": 0.5,
            # Present so the test can prove the router strips them: the
            # browser draws the chart from /api/chart at the window it is
            # showing, and a second series here is a second answer to "what
            # was the last bar".
            "candles": [{"ts": 1.0}], "weekly_candles": [], "daily_candles": [],
            # `trigger_candles` is one of them and does not end in the same
            # word as the others -- which is exactly how it survived the free
            # read's filter while the billable one dropped it.
            "trigger_candles": [{"ts": 1.0}],
        },
        "candidate": _candidate(),
        "why": "",
        "settings": {"setforget_lot_size": 0.0, "risk_per_trade_pct": 1.0},
        "writes": [],
        "cfg": {"ai_provider": "claude", "claude_model": "a-model"},
        "configured": True,
        "target": "local",
        "read_raises": None,
        "evaluate_raises": None,
        "ai": {"verdict": "take", "reasoning": "Daily demand.",
               "levels_rejected": [], "error": None},
    }

    async def _read(engine):
        if state["read_raises"]:
            raise state["read_raises"]
        return dict(state["evidence"])

    async def _evaluate(engine, cfg, timeout=60):
        if state["evaluate_raises"]:
            raise state["evaluate_raises"]
        return {
            "generated_at": 1.0, "price": state["evidence"]["price"],
            "evidence": {k: v for k, v in state["evidence"].items()
                         if not k.endswith("candles")},
            "candidate": state["candidate"], "no_setup_reason": state["why"],
            "confluence": {"items": [], "score": 7, "max": 9, "pct": 77.8,
                           "grade": "high"},
            "ai": state["ai"], "billed": True, "invalidations": [],
        }

    monkeypatch.setattr(sf_router.sf_ctl, "read_chart", _read)
    monkeypatch.setattr(sf_router.sf_ctl, "evaluate", _evaluate)
    monkeypatch.setattr(sf_router.sf_ctl, "propose",
                        lambda ev: (state["candidate"], state["why"]))
    monkeypatch.setattr(sf_router.sf_ctl, "score",
                        lambda ev, candidate: {"items": [], "score": 7, "max": 9,
                                               "pct": 77.8, "grade": "high"})
    monkeypatch.setattr(sf_router.sf_ctl, "get_risk_settings",
                        lambda: dict(state["settings"]))
    monkeypatch.setattr(sf_router.sf_ctl, "update_risk_settings",
                        lambda v: (state["writes"].append(v),
                                   state["settings"].update(v)))
    monkeypatch.setattr(sf_router.settings_ctl, "load_config",
                        lambda: dict(state["cfg"]))
    monkeypatch.setattr(sf_router.ai_ctl, "is_configured",
                        lambda cfg: state["configured"])
    monkeypatch.setattr(sf_router.engines_ctl, "control_target",
                        lambda: state["target"])
    return state


def _message(res) -> str:
    return res.json()["error"]["message"]


class TestItCannotPlaceAnything:
    """The reason this module is a separate router from `orders.py`."""

    def test_no_endpoint_here_reaches_the_engines_money_methods(
        self, make_client, sf, sentinel_engine,
    ):
        client = make_client()
        client.get("/api/trading/setforget")
        client.post("/api/trading/setforget/evaluate")
        client.put("/api/trading/setforget/settings", json={"lot_size": 0.05})

        touched = [n for n, _, _ in sentinel_engine.calls if n in _MONEY_CALLS]
        assert touched == []

    def test_the_module_offers_no_execute_endpoint(self, make_client, sf):
        """A negative control. If someone adds `/execute` here later, the test
        above would still pass — its sentinel would simply record a call
        nobody made yet. This is the one that goes red."""
        paths = {r.path for r in sf_router.router.routes}

        assert not any("execute" in p or "order" in p for p in paths), paths

    def test_every_route_here_is_a_read_or_a_setting(self, make_client, sf):
        methods = {(r.path, m) for r in sf_router.router.routes
                   for m in r.methods if m not in ("HEAD", "OPTIONS")}

        assert methods == {
            ("/api/trading/setforget", "GET"),
            ("/api/trading/setforget/evaluate", "POST"),
            ("/api/trading/setforget/settings", "PUT"),
        }


class TestTheFreeRead:
    def test_it_carries_the_candidate_and_the_checklist(self, make_client, sf):
        body = make_client().get("/api/trading/setforget").json()

        assert body["candidate"]["direction"] == "BUY"
        assert body["confluence"]["max"] == 9
        assert body["billed"] is False
        assert body["ai"] is None

    def test_it_does_not_ship_a_second_copy_of_the_candles(self, make_client, sf):
        """The browser draws the chart from /api/chart at the window it is
        showing. A series here too would put two on one page that can disagree
        about what the last bar was."""
        body = make_client().get("/api/trading/setforget").json()

        assert "candles" not in body["evidence"]
        assert "weekly_candles" not in body["evidence"]
        # Every series, named or not. Until 2026-09-22 this route filtered by
        # a list it spelled out itself rather than asking the service, so when
        # `trigger_candles` was added to the evidence the billable read
        # dropped it and the free one -- polled every sixty seconds -- shipped
        # it. Two reads of the same chart, two different payloads.
        assert "trigger_candles" not in body["evidence"]
        assert body["evidence"]["weekly_bias"] == "bullish"

    def test_no_setup_is_a_state_with_a_reason_not_an_empty_card(
        self, make_client, sf,
    ):
        """"No setup" on a page the operator just pressed a button on is
        indistinguishable from a broken one."""
        sf["candidate"] = None
        sf["why"] = "The Weekly (bullish) and the Daily (bearish) disagree."

        body = make_client().get("/api/trading/setforget").json()

        assert body["candidate"] is None
        assert "disagree" in body["no_setup_reason"]

    def test_it_says_where_an_order_would_land_and_which_model_would_bill(
        self, make_client, sf,
    ):
        sf["target"] = "remote"

        body = make_client().get("/api/trading/setforget").json()

        assert body["control_target"] == "remote"
        assert body["ai_model"] == "a-model"
        assert body["ai_configured"] is True

    def test_the_named_model_follows_the_selected_provider(self, make_client, sf):
        """`claude_model` has a config default that is set even when DeepSeek
        is the selected provider, so a naive `claude_model or deepseek_model`
        told the page a Claude model was about to be billed when it was not.
        """
        sf["cfg"] = {"ai_provider": "deepseek",
                     "deepseek_model": "deepseek-flash",
                     "claude_model": "claude-sonnet-4-6"}

        body = make_client().get("/api/trading/setforget").json()

        assert body["ai_provider"] == "deepseek"
        assert body["ai_model"] == "deepseek-flash"

    def test_a_chart_that_cannot_be_read_says_why(self, make_client, sf):
        sf["read_raises"] = RuntimeError("the bridge is not connected")

        res = make_client().get("/api/trading/setforget")

        assert res.status_code == 409
        assert "not connected" in _message(res)


class TestTheRulesAreAppliedOnTheFreeReadToo:
    """Found by looking at the running page, not by reading the code.

    `propose` builds the nearest-zone candidate it can; whether that candidate
    is TRADEABLE is `setup.invalidations`' job, and the free read was returning
    an empty list unconditionally. A 1:0.05 setup -- entry and target a few
    ticks apart, which is what happens when the next opposing zone sits right
    on top of the entry -- therefore arrived on screen with no refusal on it
    and an Execute button that was not disabled. It renders exactly as tidily
    as a 1:3 one.
    """

    def test_a_setup_that_breaks_the_rules_says_so_without_paying_for_a_review(
        self, make_client, sf,
    ):
        sf["candidate"] = _candidate(take_profit=1986.0, reward=1.0, rr=0.08)

        body = make_client().get("/api/trading/setforget").json()

        assert body["billed"] is False
        assert body["invalidations"], "a 1:0.08 setup came back with no refusal"
        assert any("1:2" in r for r in body["invalidations"])

    def test_a_sound_setup_has_none(self, make_client, sf):
        body = make_client().get("/api/trading/setforget").json()

        assert body["invalidations"] == []

    def test_no_candidate_has_nothing_to_invalidate(self, make_client, sf):
        sf["candidate"] = None

        assert make_client().get("/api/trading/setforget").json()["invalidations"] == []


class TestSizing:
    def test_the_per_lot_cash_figures_come_back_for_the_position_box(
        self, make_client, sf,
    ):
        """The box re-prices as the operator moves the lot selector, so the
        browser multiplies a per-lot figure rather than re-deriving P&L in
        TypeScript — which would be a second answer to what a trade is worth."""
        body = make_client().get("/api/trading/setforget").json()

        assert body["risk_per_lot"] == pytest.approx(1300.0)
        assert body["reward_per_lot"] == pytest.approx(5500.0)

    def test_the_suggested_lot_is_sized_from_the_balance_and_the_stop(
        self, make_client, sf, sentinel_engine, fresh_db,
    ):
        # `fresh_db` because the shared sizer reads Max Risk per trade % from
        # the risk settings table -- the second ceiling this section must not
        # bypass by doing its own arithmetic.
        sentinel_engine.account = {"login": 1, "is_demo": True, "balance": 5000.0}

        body = make_client().get("/api/trading/setforget").json()

        assert body["balance"] == pytest.approx(5000.0)
        assert body["suggested_lot"] > 0

    def test_an_unreadable_balance_suggests_nothing_rather_than_a_number(
        self, make_client, sf, sentinel_engine,
    ):
        """A lot size computed against an invented balance looks exactly like
        a real one, and it would go to a broker."""
        sentinel_engine.account = {"login": 1, "is_demo": True}

        body = make_client().get("/api/trading/setforget").json()

        assert body["balance"] is None
        assert body["suggested_lot"] is None

    def test_no_candidate_means_no_sizing_at_all(self, make_client, sf):
        sf["candidate"] = None

        body = make_client().get("/api/trading/setforget").json()

        assert body["suggested_lot"] is None
        assert body["risk_per_lot"] is None


class TestTheBillableRead:
    def test_it_returns_the_same_shape_as_the_free_one_plus_the_review(
        self, make_client, sf,
    ):
        client = make_client()
        free = client.get("/api/trading/setforget").json()
        billed = client.post("/api/trading/setforget/evaluate").json()

        assert set(free) == set(billed)
        assert billed["billed"] is True
        assert billed["ai"]["verdict"] == "take"

    def test_without_a_provider_it_refuses_and_says_the_page_still_works(
        self, make_client, sf,
    ):
        sf["configured"] = False

        res = make_client().post("/api/trading/setforget/evaluate")

        assert res.status_code == 409
        assert "Settings > AI" in _message(res)
        assert "without one" in _message(res)

    def test_a_failure_inside_the_evaluation_is_a_refusal_with_the_reason(
        self, make_client, sf,
    ):
        sf["evaluate_raises"] = RuntimeError("no candles")

        res = make_client().post("/api/trading/setforget/evaluate")

        assert res.status_code == 409
        assert "no candles" in _message(res)


class TestTheSetting:
    def test_the_lot_size_is_stored_under_its_own_key(self, make_client, sf):
        make_client().put("/api/trading/setforget/settings", json={"lot_size": 0.05})

        assert sf["writes"] == [{"setforget_lot_size": 0.05}]

    def test_zero_is_allowed_and_means_size_it_from_risk(self, make_client, sf):
        res = make_client().put("/api/trading/setforget/settings",
                                json={"lot_size": 0})

        assert res.status_code == 200
        assert sf["writes"] == [{"setforget_lot_size": 0.0}]

    def test_a_negative_lot_is_refused_with_the_alternative(self, make_client, sf):
        res = make_client().put("/api/trading/setforget/settings",
                                json={"lot_size": -1})

        assert res.status_code == 400
        assert "0" in _message(res)
        assert sf["writes"] == []

    def test_an_empty_body_changes_nothing(self, make_client, sf):
        res = make_client().put("/api/trading/setforget/settings", json={})

        assert res.status_code == 409
        assert sf["writes"] == []
