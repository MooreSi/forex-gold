"""`/api/ai/research` -- the AI Analysis tab's single call.

The NiceGUI tab had one button. It gathered the tick, the candles, the day's
signals, the account's performance and the strategy catalogue, asked the model
ONCE, and rendered a structured answer: sentiment with a confidence, a price
range, the drivers, the risks, the levels and a strategy recommendation. The
React port never ported it -- its AI tab asks a separate question per subject
and prints whatever prose comes back. Asked for on 2026-09-20.

Two rules. Reading what was last found costs nothing, so opening the tab is
free; and asking with no provider configured refuses out loud rather than
returning an empty answer, which looks like a model with no opinion.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import ai as ai_router


@pytest.fixture
def lab(monkeypatch, sentinel_engine):
    state = {
        "configured": True,
        "runs": 0,
        "stored": {"analysis": {"sentiment": "bullish"}, "saved_at": "2026-09-20T09:00:00Z"},
        "result": {"analysis": {"sentiment": "bearish", "summary": "Gold is offered."},
                   "saved_at": "2026-09-20T10:00:00Z"},
    }

    async def _run(engine, **kw):
        state["runs"] += 1
        if isinstance(state["result"], Exception):
            raise state["result"]
        return dict(state["result"])

    monkeypatch.setattr(ai_router.ai_ctl, "is_configured",
                        lambda cfg: state["configured"])
    monkeypatch.setattr(ai_router.ai_ctl, "run_market_research", _run)
    monkeypatch.setattr(ai_router.ai_ctl, "last_market_research",
                        lambda: dict(state["stored"]))
    monkeypatch.setattr(ai_router.settings_ctl, "load_config", lambda: {})
    return state


class TestReadingTheLastOneIsFree:
    def test_the_stored_analysis_is_returned(self, make_client, lab):
        body = make_client().get("/api/ai/research").json()

        assert body["analysis"]["sentiment"] == "bullish"

    def test_no_model_is_asked(self, make_client, lab):
        make_client().get("/api/ai/research")

        assert lab["runs"] == 0

    def test_it_says_it_is_not_billable(self, make_client, lab):
        """The rule of this router: a cost the browser cannot see is a cost
        that gets dropped in a redesign."""
        assert make_client().get("/api/ai/research").json()["billable"] is False

    def test_an_install_that_has_never_researched_says_so_plainly(
        self, make_client, lab,
    ):
        lab["stored"] = {"analysis": None, "saved_at": ""}

        body = make_client().get("/api/ai/research").json()

        assert body["analysis"] is None


class TestAskingForANewOne:
    def test_it_runs_the_research(self, make_client, lab):
        make_client().post("/api/ai/research", json={})

        assert lab["runs"] == 1

    def test_one_request_is_one_model_call(self, make_client, lab):
        """The whole point: one call across every metric, not one per
        subject."""
        make_client().post("/api/ai/research", json={})

        assert lab["runs"] == 1

    def test_the_analysis_comes_back(self, make_client, lab):
        body = make_client().post("/api/ai/research", json={}).json()

        assert body["analysis"]["sentiment"] == "bearish"

    def test_it_says_it_is_billable(self, make_client, lab):
        assert make_client().post("/api/ai/research", json={}).json()["billable"] is True

    def test_no_provider_refuses_out_loud(self, make_client, lab):
        """An empty answer reads as a model with no opinion, which is a
        different and much more interesting result."""
        lab["configured"] = False

        r = make_client().post("/api/ai/research", json={})

        assert r.status_code >= 400
        assert "Settings" in r.json()["error"]["message"]

    def test_no_provider_asks_for_nothing(self, make_client, lab):
        lab["configured"] = False

        make_client().post("/api/ai/research", json={})

        assert lab["runs"] == 0
