"""The Signal Generator tab: three engines that produce signals of their own.

Starting an engine does not place an order, but it is the switch that lets one
be placed with nobody watching — so the two assertions that matter are about
what the tab is allowed to start, and what it is never allowed to call.

* **Named, not bulk.** The registry's bulk start exists for the app's own
  startup and deliberately skips Bounce. A UI button wired to it would start
  engines the operator did not ask for.
* **Never the blocking fit.** A five-second RandomForest train started from a
  UI handler freezes the event loop, the EA socket reader and the monitor loop
  together; the EA reconnects after ten seconds of Python silence.

Nothing here touches a broker: the engines are stand-ins that record start()
and stop().
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import engines as engines_router


class _Engine:
    def __init__(self) -> None:
        self.is_running = False
        self.calls: list[str] = []

    def start(self) -> None:
        self.calls.append("start")
        self.is_running = True

    def stop(self) -> None:
        self.calls.append("stop")
        self.is_running = False


@pytest.fixture
def engines(monkeypatch):
    state = {
        "instances": {"breakout": _Engine(), "bounce": None, "reversal": _Engine()},
        "settings": {"re_min_adx": 22},
        "pro_model": {"trained": True, "samples": 412},
        "bulk": [],
        "fits": [],
        "written": [],
        "applied": [],
    }

    async def _settings_async():
        return state["settings"]

    async def _realised():
        return {"net_pnl": 88.4}

    async def _study():
        return "## Reversal study\n12 signals."

    async def _recommend():
        return {"re_min_adx": 25}

    monkeypatch.setattr(engines_router.engines_ctl, "get_engine",
                        lambda name: state["instances"].get(name))
    monkeypatch.setattr(engines_router.engines_ctl, "engines_running",
                        lambda: {k: bool(getattr(v, "is_running", False))
                                 for k, v in state["instances"].items()})
    monkeypatch.setattr(engines_router.engines_ctl, "get_risk_settings_async", _settings_async)
    monkeypatch.setattr(engines_router.engines_ctl, "update_risk_settings",
                        lambda f: state["written"].append(f))
    monkeypatch.setattr(engines_router.engines_ctl, "pro_model_status",
                        lambda: state["pro_model"])
    monkeypatch.setattr(engines_router.engines_ctl, "pro_model_fit_in_background",
                        lambda force=False: state["fits"].append(("background", force)))
    monkeypatch.setattr(engines_router.engines_ctl, "pro_model_fit",
                        lambda *a, **k: state["fits"].append(("blocking", a, k)))
    # The bulk pair lives in the engine registry, not on the controller: its
    # only caller is the Local/Remote handover, and a service may not import a
    # controller. Recorded here so the tab can still be shown never to use it.
    from backend.src.services.engines import registry as _registry
    monkeypatch.setattr(_registry, "start_stopped",
                        lambda: state["bulk"].append("start_all"))
    monkeypatch.setattr(_registry, "stop_running",
                        lambda: state["bulk"].append("stop_all"))
    monkeypatch.setattr(engines_router.engines_ctl, "reversal_realised_pnl", _realised)
    monkeypatch.setattr(engines_router.engines_ctl, "reversal_shadow_report",
                        lambda: [{"signal_id": "s1"}])
    monkeypatch.setattr(engines_router.engines_ctl, "reversal_research_study", _study)
    monkeypatch.setattr(engines_router.engines_ctl, "reversal_ai_recommend", _recommend)
    monkeypatch.setattr(engines_router.engines_ctl, "reversal_ai_apply",
                        lambda s: state["applied"].append(s) or {"re_min_adx": 25})
    return state


# ── What is running ──────────────────────────────────────────────────────────

def test_the_tab_lists_all_three_engines_by_the_name_the_operator_uses(make_client, engines):
    """`test_panel.py` was the Bounce engine — the standing example in this
    repo of what naming a surface after its service costs."""
    rows = make_client().get("/api/engines/state").json()["engines"]

    assert [r["id"] for r in rows] == ["breakout", "bounce", "reversal"]
    assert [r["label"] for r in rows] == ["Breakout", "Bounce", "Reversal"]


def test_an_engine_that_is_not_built_reads_differently_from_one_that_is_stopped(
    make_client, engines,
):
    """Both show "not running". Only one of them can be started."""
    rows = {r["id"]: r for r in make_client().get("/api/engines/state").json()["engines"]}

    assert rows["bounce"]["built"] is False
    assert rows["bounce"]["running"] is False
    assert rows["breakout"]["built"] is True
    assert rows["breakout"]["running"] is False


def test_a_running_engine_is_reported_as_running(make_client, engines):
    engines["instances"]["breakout"].is_running = True

    rows = {r["id"]: r for r in make_client().get("/api/engines/state").json()["engines"]}

    assert rows["breakout"]["running"] is True


def test_the_state_carries_the_settings_and_the_model_status(make_client, engines):
    body = make_client().get("/api/engines/state").json()

    assert body["settings"] == {"re_min_adx": 22}
    assert body["pro_model"] == {"trained": True, "samples": 412}


# ── Starting and stopping ────────────────────────────────────────────────────

def test_starting_one_engine_starts_only_that_one(make_client, engines):
    make_client().post("/api/engines/running",
                       json={"engine": "breakout", "running": True})

    assert engines["instances"]["breakout"].calls == ["start"]
    assert engines["instances"]["reversal"].calls == []


def test_stopping_one_engine_stops_only_that_one(make_client, engines):
    engines["instances"]["reversal"].is_running = True

    make_client().post("/api/engines/running",
                       json={"engine": "reversal", "running": False})

    assert engines["instances"]["reversal"].calls == ["stop"]
    assert engines["instances"]["breakout"].calls == []


def test_the_tab_never_uses_the_bulk_start(make_client, engines):
    """The registry's bulk start skips Bounce and is for the app's own startup
    and the Local/Remote handover. A button wired to it would start engines
    nobody asked for."""
    client = make_client()
    client.post("/api/engines/running", json={"engine": "breakout", "running": True})
    client.post("/api/engines/running", json={"engine": "reversal", "running": False})

    assert engines["bulk"] == []


def test_starting_an_engine_that_is_not_built_is_refused_with_a_reason(
    make_client, engines,
):
    r = make_client().post("/api/engines/running",
                           json={"engine": "bounce", "running": True})

    assert r.status_code == 409
    assert "not built on this install" in r.json()["error"]["message"]


def test_an_unknown_engine_is_refused_by_name(make_client, engines):
    r = make_client().post("/api/engines/running",
                           json={"engine": "teapot", "running": True})

    assert r.status_code == 400
    assert "teapot" in r.json()["error"]["message"]


def test_the_running_switch_is_not_reachable_by_GET(make_client, engines):
    assert make_client().get("/api/engines/running").status_code == 405


# ── The pro-signal model ─────────────────────────────────────────────────────

def test_a_refit_is_started_in_the_background_never_inline(make_client, engines):
    """The assertion this endpoint exists for. bugs/030: an inline fit freezes
    the UI, the EA socket reader and the monitor loop together."""
    body = make_client().post("/api/engines/reversal/fit").json()

    assert engines["fits"] == [("background", True)]
    assert body["started"] is True


def test_no_module_in_the_api_layer_calls_the_blocking_fit():
    """Structural, so a future endpoint cannot reintroduce it."""
    import pathlib

    api = pathlib.Path(engines_router.__file__).parent
    sources = list(api.rglob("*.py"))
    assert sources, "the API layer has no Python in it — this scan is inert"
    for path in sources:
        body = "\n".join(l for l in path.read_text(encoding="utf-8").splitlines()
                         if not l.strip().startswith("#"))
        assert "pro_model_fit(" not in body, path


# ── Reversal extras ──────────────────────────────────────────────────────────

def test_the_reversal_report_reads_history_and_places_nothing(make_client, engines):
    body = make_client().get("/api/engines/reversal/report").json()

    assert body["realised"] == {"net_pnl": 88.4}
    assert body["shadow"] == [{"signal_id": "s1"}]
    assert engines["instances"]["reversal"].calls == []


def test_an_ai_recommendation_says_it_is_billable_and_writes_nothing(make_client, engines):
    body = make_client().post("/api/engines/reversal/ai/recommend").json()

    assert body["billable"] is True
    assert body["recommendation"] == {"re_min_adx": 25}
    assert engines["applied"] == [], "a recommendation wrote a live setting"


def test_applying_a_recommendation_goes_through_the_controllers_sanitiser(
    make_client, engines,
):
    """"The allowlist is the only thing standing between a model's output and a
    live trading setting." This layer must not write the settings itself."""
    make_client().post("/api/engines/reversal/ai/apply",
                       json={"settings": {"re_min_adx": 25, "evil": 1}})

    assert engines["applied"] == [{"re_min_adx": 25, "evil": 1}]
    assert engines["written"] == [], "the router wrote settings around the sanitiser"


def test_a_settings_write_forwards_only_what_it_was_given(make_client, engines):
    make_client().put("/api/engines/settings", json={"re_min_adx": 30})

    assert engines["written"] == [{"re_min_adx": 30}]
