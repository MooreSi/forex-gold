"""Settings > AI: which model answers, and the key that lets it.

Restored 2026-09-18. The React port shipped without this surface, so on a fresh
install there was no way to enter an API key from the dashboard and every AI
feature in the app was unreachable unless somebody edited `config.yaml`.

Three properties are worth more than the routing:

  * **A key is never echoed back.** Like every other credential in this layer.
  * **A key can be tested before it is saved.** Otherwise "test before you
    commit" is impossible, and the operator saves an unverified key and finds
    out when an analysis fails hours later.
  * **A local save survives a paired node that cannot be reached.** The push is
    what makes "the provider I picked" apply on both machines; it is not worth
    losing the setting on this one.

Nothing here contacts a model: `complete` and `fetch_available_models` are
recorders, and no test spends a token.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import ai_settings as ai_router


@pytest.fixture
def ai(monkeypatch):
    state = {
        "cfg": {
            "ai_provider": "claude",
            "anthropic_api_key": "sk-stored",
            "claude_model": "claude-sonnet-4-6",
            "claude_models_cache": ["claude-sonnet-4-6"],
        },
        "saved": [],
        "pushed": [],
        "completions": [],
        "models": ["m1", "m2"],
        "complete_raises": None,
        "models_raises": None,
        "push_raises": None,
    }

    async def _complete(cfg, system, prompt, max_tokens, timeout=30):
        state["completions"].append({"cfg": cfg, "prompt": prompt,
                                     "max_tokens": max_tokens})
        if state["complete_raises"]:
            raise state["complete_raises"]
        return "pong"

    async def _models(provider, api_key):
        state["completions"].append({"models_for": provider, "key": api_key})
        if state["models_raises"]:
            raise state["models_raises"]
        return list(state["models"])

    async def _push(updates):
        if state["push_raises"]:
            raise state["push_raises"]
        state["pushed"].append(updates)

    monkeypatch.setattr(ai_router.settings_ctl, "load_config",
                        lambda: dict(state["cfg"]))
    monkeypatch.setattr(ai_router.settings_ctl, "save_config",
                        lambda v: (state["saved"].append(v), state["cfg"].update(v)))
    monkeypatch.setattr(ai_router.ai_ctl, "complete", _complete)
    monkeypatch.setattr(ai_router.ai_ctl, "fetch_available_models", _models)
    monkeypatch.setattr(ai_router.ai_ctl, "is_configured",
                        lambda cfg: bool(cfg.get("anthropic_api_key")
                                         or cfg.get("deepseek_api_key")))
    monkeypatch.setattr(ai_router.sync_ctl, "push_ai_config", _push)
    return state


def _message(res) -> str:
    return res.json()["error"]["message"]


class TestReading:
    def test_no_key_reaches_the_browser(self, make_client, ai):
        assert "sk-stored" not in make_client().get("/api/ai/settings").text

    def test_it_says_which_keys_are_stored(self, make_client, ai):
        body = make_client().get("/api/ai/settings").json()

        assert body["anthropic_api_key_set"] is True
        assert body["deepseek_api_key_set"] is False

    def test_it_offers_a_cached_model_list_before_anybody_refreshes(
        self, make_client, ai,
    ):
        """An empty dropdown on the screen you open to type a key reads as
        "this provider has no models"."""
        body = make_client().get("/api/ai/settings").json()

        assert body["claude_models"] == ["claude-sonnet-4-6"]

    def test_deepseek_falls_back_to_a_known_list(self, make_client, ai):
        body = make_client().get("/api/ai/settings").json()

        assert body["deepseek_models"], "no models to choose from at all"


class TestWriting:
    def test_it_saves_what_it_was_given(self, make_client, ai):
        make_client().put("/api/ai/settings", json={"provider": "deepseek"})

        assert ai["saved"] == [{"provider": "deepseek"}]

    def test_a_blank_key_does_not_erase_the_stored_one(self, make_client, ai):
        """The field is empty on screen whether or not one is set, so passing
        it through would wipe a working key on every unrelated edit."""
        make_client().put("/api/ai/settings",
                          json={"provider": "claude", "anthropic_api_key": ""})

        assert ai["saved"] == [{"provider": "claude"}]

    def test_an_unknown_provider_is_refused(self, make_client, ai):
        res = make_client().put("/api/ai/settings", json={"provider": "oracle"})

        assert res.status_code == 400
        assert ai["saved"] == []

    def test_it_tells_the_paired_node(self, make_client, ai):
        """Settings > AI is per-node — a separate config.yaml on each side,
        unlike the risk settings. Without this, the provider applies only on
        whichever dashboard happened to be open."""
        make_client().put("/api/ai/settings", json={"claude_model": "opus"})

        assert ai["pushed"] == [{"claude_model": "opus"}]

    def test_a_node_that_cannot_be_reached_does_not_lose_the_local_save(
        self, make_client, ai,
    ):
        ai["push_raises"] = TimeoutError("no route")

        res = make_client().put("/api/ai/settings", json={"claude_model": "opus"})

        assert res.status_code == 200
        assert ai["saved"] == [{"claude_model": "opus"}]

    def test_an_empty_body_is_refused_rather_than_writing_nothing_quietly(
        self, make_client, ai,
    ):
        res = make_client().put("/api/ai/settings", json={})

        assert res.status_code == 409
        assert ai["saved"] == []


class TestTestingAKey:
    def test_it_uses_the_typed_key_rather_than_the_stored_one(self, make_client, ai):
        """The whole point of the button: check before you commit."""
        make_client().post("/api/ai/settings/test",
                           json={"provider": "claude", "api_key": "sk-typed"})

        assert ai["completions"][0]["cfg"]["anthropic_api_key"] == "sk-typed"

    def test_with_no_typed_key_it_uses_the_stored_one(self, make_client, ai):
        make_client().post("/api/ai/settings/test", json={"provider": "claude"})

        assert ai["completions"][0]["cfg"]["anthropic_api_key"] == "sk-stored"

    def test_it_spends_as_little_as_it_can(self, make_client, ai):
        """It is billable. Proving a key works needs a handful of tokens, not
        an answer."""
        make_client().post("/api/ai/settings/test", json={"provider": "claude"})

        assert ai["completions"][0]["max_tokens"] <= 5

    def test_it_says_it_is_billable(self, make_client, ai):
        body = make_client().post("/api/ai/settings/test",
                                  json={"provider": "claude"}).json()

        assert body["billable"] is True

    def test_with_no_key_at_all_it_refuses_rather_than_calling(self, make_client, ai):
        ai["cfg"] = {"ai_provider": "deepseek"}

        res = make_client().post("/api/ai/settings/test", json={"provider": "deepseek"})

        assert res.status_code == 409
        assert ai["completions"] == []

    def test_a_rejected_key_is_reported_with_the_provider_s_reason(
        self, make_client, ai,
    ):
        ai["complete_raises"] = RuntimeError("401 invalid x-api-key")

        res = make_client().post("/api/ai/settings/test", json={"provider": "claude"})

        assert res.status_code == 409
        assert "invalid x-api-key" in _message(res)

    def test_it_never_writes_anything(self, make_client, ai):
        """Negative control. Testing is not saving; a test that saved would
        commit a key the operator was only checking."""
        make_client().post("/api/ai/settings/test",
                           json={"provider": "claude", "api_key": "sk-typed"})

        assert ai["saved"] == []


class TestRefreshingTheModelList:
    def test_it_caches_what_came_back(self, make_client, ai):
        body = make_client().post("/api/ai/settings/models",
                                  json={"provider": "claude"}).json()

        assert body["models"] == ["m1", "m2"]
        assert ai["saved"] == [{"claude_models_cache": ["m1", "m2"]}]

    def test_an_empty_list_is_refused_rather_than_cached(self, make_client, ai):
        """Caching an empty list would leave the dropdown permanently empty
        and look like the provider has no models."""
        ai["models"] = []

        res = make_client().post("/api/ai/settings/models", json={"provider": "claude"})

        assert res.status_code == 409
        assert ai["saved"] == []

    def test_a_failed_fetch_keeps_the_old_cache(self, make_client, ai):
        ai["models_raises"] = RuntimeError("connection reset")

        res = make_client().post("/api/ai/settings/models", json={"provider": "claude"})

        assert res.status_code == 409
        assert ai["saved"] == []


@pytest.mark.parametrize("path", ["/api/ai/settings/test", "/api/ai/settings/models"])
def test_neither_is_reachable_by_a_get(path, make_client, ai):
    """Both cost money or make a network call."""
    assert make_client().get(path).status_code == 405
    assert ai["completions"] == []
