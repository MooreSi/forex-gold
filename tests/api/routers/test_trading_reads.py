"""The Trading tab's reads, and the two controls that stop and start it.

**Nothing here places or closes a position.** The order paths live in
`test_orders.py` behind their own router, deliberately, so that a GET can never
open one. What is here is everything else the tab does: editing a pending
signal, reading channel-strategy recommendations, and pausing trading.

Pausing is the safe direction and is not gated. Resuming lets automated entries
happen again, which is why it goes through a service that does more than clear
a flag — see `tests/risk/test_manual_pause.py`.
"""
from __future__ import annotations

import pytest

# ── Editing a pending signal ─────────────────────────────────────────────────

class TestEditingAPendingSignal:
    """The row-editor bug, guarded at the boundary this time.

    The NiceGUI editor read fifteen widgets from an enclosing loop, so without
    explicit captures Save on one row wrote another row's values. React has its
    own version of that (a handler closing over state from an earlier render),
    and the structural defence is the same either way: **the signal id comes
    from the PATH, never from the body.** A payload-supplied id is the thing a
    stale closure can get wrong.
    """

    def test_the_id_comes_from_the_path(self, make_client, sentinel_engine):
        async def _update(signal_id, **fields):
            sentinel_engine.calls.append(("update_signal", (signal_id,), fields))

        sentinel_engine.update_signal = _update

        make_client().put("/api/trading/signals/S-42",
                          json={"stop_loss": 2421.0, "tp1": 2440.0})

        args, kwargs = sentinel_engine.call_named("update_signal")
        assert args == ("S-42",)
        assert kwargs == {"stop_loss": 2421.0, "tp1": 2440.0}

    def test_an_id_in_the_body_cannot_redirect_the_write(self, make_client, sentinel_engine):
        """The assertion that makes the path-id choice worth anything."""
        async def _update(signal_id, **fields):
            sentinel_engine.calls.append(("update_signal", (signal_id,), fields))

        sentinel_engine.update_signal = _update

        make_client().put("/api/trading/signals/S-42",
                          json={"signal_id": "S-99", "stop_loss": 2421.0})

        args, _kwargs = sentinel_engine.call_named("update_signal")
        assert args == ("S-42",)

    def test_deleting_a_telegram_signal_row_forwards_its_id(
        self, make_client, monkeypatch,
    ):
        from backend.src.api.routers import trading as trading_router

        deleted = []
        monkeypatch.setattr(trading_router.trading_ctl, "delete_tg_signal_row",
                            lambda row_id: deleted.append(row_id))

        make_client().delete("/api/trading/tg-signals/17")

        assert deleted == [17]


# ── Channel strategy recommendations ─────────────────────────────────────────

class TestChannelStrategyRecommendations:
    """One free read and one billable ask, separated the same way the AI tab
    separates them. A screen that could only see a recommendation by paying for
    a new one would charge for every glance."""

    def test_reading_the_stored_recommendations_costs_nothing(
        self, make_client, monkeypatch,
    ):
        from backend.src.api.routers import trading as trading_router

        asked = []
        monkeypatch.setattr(trading_router.trading_ctl, "get_channel_strategy_rec_map",
                            lambda sources: {"GoldSignals": "scale_out"})
        monkeypatch.setattr(trading_router.trading_ctl, "get_channel_strategy_recs",
                            lambda sources: asked.append(sources))
        monkeypatch.setattr(trading_router.trading_ctl, "build_strategy_catalogue",
                            lambda **k: [{"key": "scale_out", "label": "Scale out",
                                          "summary": "Take part off at TP1."}])

        body = make_client().get(
            "/api/trading/channel-strategies/recommendations?sources=GoldSignals").json()

        assert body["billable"] is False
        assert body["recommendations"]["GoldSignals"]["strategy"] == "scale_out"
        assert asked == []

    def test_each_recommendation_carries_its_human_label(
        self, make_client, monkeypatch,
    ):
        """So the browser never holds a mapping from strategy id to name: one
        place to change when an id is renamed."""
        from backend.src.api.routers import trading as trading_router

        monkeypatch.setattr(trading_router.trading_ctl, "get_channel_strategy_rec_map",
                            lambda sources: {"GoldSignals": "scale_out"})
        monkeypatch.setattr(trading_router.trading_ctl, "build_strategy_catalogue",
                            lambda **k: [{"key": "scale_out", "label": "Scale out",
                                          "summary": "Take part off at TP1."}])

        rec = make_client().get(
            "/api/trading/channel-strategies/recommendations?sources=GoldSignals"
        ).json()["recommendations"]["GoldSignals"]

        assert rec["label"] == "Scale out"
        assert rec["summary"] == "Take part off at TP1."

    def test_a_strategy_this_build_no_longer_has_renders_as_itself(
        self, make_client, monkeypatch,
    ):
        """Not as blank. A stored recommendation naming a retired strategy is
        a real state, and an empty cell reads as "no recommendation"."""
        from backend.src.api.routers import trading as trading_router

        monkeypatch.setattr(trading_router.trading_ctl, "get_channel_strategy_rec_map",
                            lambda sources: {"GoldSignals": "retired_thing"})
        monkeypatch.setattr(trading_router.trading_ctl, "build_strategy_catalogue",
                            lambda **k: [{"key": "scale_out", "label": "Scale out",
                                          "summary": ""}])

        rec = make_client().get(
            "/api/trading/channel-strategies/recommendations?sources=GoldSignals"
        ).json()["recommendations"]["GoldSignals"]

        assert rec["label"] == "retired_thing"

    def test_asking_for_a_new_recommendation_says_it_is_billable(
        self, make_client, monkeypatch,
    ):
        from backend.src.api.routers import trading as trading_router

        async def _recs(sources):
            return {s: "be_runner" for s in sources}

        monkeypatch.setattr(trading_router.trading_ctl, "get_channel_strategy_recs", _recs)

        body = make_client().post("/api/trading/channel-strategies/recommend",
                                  json={"sources": ["GoldSignals"]}).json()

        assert body["billable"] is True
        assert body["recommendations"] == {"GoldSignals": "be_runner"}

    def test_asking_with_no_channels_is_refused_rather_than_charged(
        self, make_client, monkeypatch,
    ):
        from backend.src.api.routers import trading as trading_router

        asked = []

        async def _recs(sources):
            asked.append(sources)
            return {}

        monkeypatch.setattr(trading_router.trading_ctl, "get_channel_strategy_recs", _recs)

        r = make_client().post("/api/trading/channel-strategies/recommend",
                               json={"sources": []})

        assert r.status_code == 400
        assert asked == []


# ── Pausing by hand ──────────────────────────────────────────────────────────

class TestPausingTrading:
    """Restored 2026-09-18. The React port shipped with no way to halt trading
    from the dashboard at all, so an operator who wanted to stop had to turn
    sources off one at a time or edit the database.

    Pausing is the safe direction. Resuming is the one that lets money move
    again, and it does MORE than clear a flag — the service re-arms the
    post-close guards, because otherwise a resume after a give-back halt lasts
    until the next trade closes."""

    def test_pausing_for_hours_forwards_the_number(self, make_client, monkeypatch):
        from backend.src.api.routers import trading as trading_router

        seen = []
        monkeypatch.setattr(trading_router.trading_ctl, "pause_trading",
                            lambda *a, **k: seen.append((a, k)) or 1_800_000_000.0)

        body = make_client().post("/api/trading/pause", json={"hours": 2}).json()

        assert seen == [((), {"hours": 2.0, "until": None})]
        assert body["paused"] is True
        assert body["until"] == 1_800_000_000.0

    def test_an_explicit_moment_wins_over_the_hours(self, make_client, monkeypatch):
        """A dialog offers both; whichever was filled in is the one meant."""
        from backend.src.api.routers import trading as trading_router

        seen = []
        monkeypatch.setattr(trading_router.trading_ctl, "pause_trading",
                            lambda *a, **k: seen.append((a, k)) or 1_800_000_000.0)

        make_client().post("/api/trading/pause",
                           json={"hours": 2, "until": 1_800_000_000.0})

        # Both are forwarded; the SERVICE decides which wins, because the rule
        # ("a moment beats a duration, and an empty box is not zero") has to
        # hold for the Telegram command too.
        assert seen == [((), {"hours": 2.0, "until": 1_800_000_000.0})]

    def test_an_empty_body_is_forwarded_as_empty_not_as_zero(
        self, make_client, monkeypatch,
    ):
        """Zero would be a pause already in the past: trading would not stop
        and the screen would say it had. The router must not invent a number
        either -- `tests/risk/test_manual_pause.py` owns the 4-hour default."""
        from backend.src.api.routers import trading as trading_router

        seen = []
        monkeypatch.setattr(trading_router.trading_ctl, "pause_trading",
                            lambda *a, **k: seen.append(k) or 1.0)

        make_client().post("/api/trading/pause", json={})

        assert seen == [{"hours": None, "until": None}]

    def test_a_moment_in_the_past_is_refused_with_its_reason(
        self, make_client, monkeypatch,
    ):
        from backend.src.api.routers import trading as trading_router

        def _boom(*a, **k):
            raise ValueError("The pause has to end in the future.")

        monkeypatch.setattr(trading_router.trading_ctl, "pause_trading", _boom)

        res = make_client().post("/api/trading/pause", json={"until": 1.0})

        assert res.status_code == 400
        assert "future" in res.json()["error"]["message"]

    def test_resuming_goes_through_the_service_that_also_rearms(
        self, make_client, monkeypatch,
    ):
        from backend.src.api.routers import trading as trading_router

        called = []
        monkeypatch.setattr(trading_router.trading_ctl, "resume_trading",
                            lambda: called.append(True))

        body = make_client().post("/api/trading/resume").json()

        assert called == [True]
        assert body["paused"] is False

    @pytest.mark.parametrize("path", ["/api/trading/pause", "/api/trading/resume"])
    def test_neither_is_reachable_by_a_get(self, path, make_client, monkeypatch):
        """Both change whether orders can be sent. A GET that does is one a
        prefetch or a refresh can fire."""
        from backend.src.api.routers import trading as trading_router

        called = []
        monkeypatch.setattr(trading_router.trading_ctl, "pause_trading",
                            lambda *a, **k: called.append("pause"))
        monkeypatch.setattr(trading_router.trading_ctl, "resume_trading",
                            lambda: called.append("resume"))

        assert make_client().get(path).status_code == 405
        assert called == []


class TestTheChannelStrategyList:
    """`GET /api/trading/channel-strategies` answered 500 on a live install.

    Found 2026-09-19 while building the Strategy screen. The controller
    returns a LIST of channels; the handler was annotated `-> dict`, and
    FastAPI validates the response against the return annotation, so every
    call raised a ResponseValidationError. Nothing in the React app had ever
    called it, which is why it went unnoticed since the port.

    The shape is now an object with a `channels` key -- the same shape every
    other read on this router uses, and one a later field can be added to
    without breaking a caller.
    """

    def test_it_answers_at_all(self, make_client, monkeypatch):
        from backend.src.api.routers import trading as trading_router
        monkeypatch.setattr(
            trading_router.trading_ctl, "get_all_channel_strategy_settings",
            lambda: [{"source": "GoldSignals", "strategy_override": None,
                      "auto_strategy": False, "lot_mult": 1.0}])

        assert make_client().get("/api/trading/channel-strategies").status_code == 200

    def test_it_returns_the_channels_the_controller_gave_it(self, make_client,
                                                            monkeypatch):
        from backend.src.api.routers import trading as trading_router
        monkeypatch.setattr(
            trading_router.trading_ctl, "get_all_channel_strategy_settings",
            lambda: [{"source": "GoldSignals", "strategy_override": "scale_out",
                      "auto_strategy": True, "lot_mult": 1.5}])

        body = make_client().get("/api/trading/channel-strategies").json()

        assert body["channels"][0]["source"] == "GoldSignals"
        assert body["channels"][0]["strategy_override"] == "scale_out"

    def test_no_channels_is_an_empty_list_not_an_error(self, make_client, monkeypatch):
        from backend.src.api.routers import trading as trading_router
        monkeypatch.setattr(
            trading_router.trading_ctl, "get_all_channel_strategy_settings",
            lambda: [])

        assert make_client().get("/api/trading/channel-strategies").json() == {
            "channels": []}
