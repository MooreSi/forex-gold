

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
