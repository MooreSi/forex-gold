"""More AI providers than Claude and DeepSeek (owner, 2026-10-21: "add other AI
providers and their available models i.e. OpenAI but not limited to").

OpenAI, Google Gemini, xAI, Mistral and OpenRouter all speak the OpenAI chat
completions protocol, so they share one adapter driven by a registry. What has
to be right:

  * each provider reads ITS key and ITS model, never another's;
  * the request goes to that provider's base URL with a bearer key;
  * OpenAI is sent `max_completion_tokens` (its reasoning models refuse
    `max_tokens`), the others `max_tokens`;
  * a reply cut off at the budget raises, as Claude's and DeepSeek's do;
  * a model list that cannot be fetched falls back rather than raising.

No test contacts a provider: httpx is given a MockTransport.
"""
from __future__ import annotations

import json

import httpx
import pytest

from backend.src.services.ai import provider as p



@pytest.fixture
def wire(monkeypatch):
    """Every httpx.AsyncClient the provider opens answers from `state`."""
    state = {"requests": [], "reply": None, "status": 200}

    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        state["requests"].append({"url": str(request.url), "method": request.method,
                                  "auth": request.headers.get("authorization"),
                                  "json": body})
        return httpx.Response(state["status"], json=state["reply"])

    real = httpx.AsyncClient

    def client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(p.httpx, "AsyncClient", client)
    monkeypatch.setattr(p, "_is_debug", lambda: False)
    return state


def _chat(text="hi", finish="stop"):
    return {"choices": [{"message": {"content": text}, "finish_reason": finish}]}


def test_the_registry_offers_openai_and_more():
    assert {"openai", "gemini", "xai", "mistral", "openrouter"} <= set(p.PROVIDERS)
    assert p.PROVIDERS[:2] == ("claude", "deepseek")


@pytest.mark.parametrize("name", ["openai", "gemini", "xai", "mistral", "openrouter"])
def test_each_is_configured_by_its_own_key_only(name):
    assert not p.is_configured({"ai_provider": name, "deepseek_api_key": "x"})
    assert p.is_configured({"ai_provider": name, f"{name}_api_key": "k"})


def test_active_model_reads_the_selected_providers_model():
    cfg = {"ai_provider": "openai", "claude_model": "claude-x", "openai_model": "gpt-x"}
    assert p.active_model(cfg) == "gpt-x"


@pytest.mark.asyncio


async def test_openai_is_called_at_its_url_with_max_completion_tokens(wire):
    wire["reply"] = _chat("pong")
    cfg = {"ai_provider": "openai", "openai_api_key": "sk-o", "openai_model": "gpt-x"}

    out = await p.complete(cfg, "sys", "ping", max_tokens=50)

    req = wire["requests"][0]
    assert out == "pong"
    assert req["url"] == "https://api.openai.com/v1/chat/completions"
    assert req["auth"] == "Bearer sk-o"
    assert req["json"]["model"] == "gpt-x"
    assert "max_tokens" not in req["json"]
    assert req["json"]["max_completion_tokens"] >= 50
    assert req["json"]["messages"][0] == {"role": "system", "content": "sys"}


@pytest.mark.parametrize("name,url", [
    ("gemini", "https://generativelanguage.googleapis.com/v1beta/openai/chat/completions"),
    ("xai", "https://api.x.ai/v1/chat/completions"),
    ("mistral", "https://api.mistral.ai/v1/chat/completions"),
    ("openrouter", "https://openrouter.ai/api/v1/chat/completions"),
])
@pytest.mark.asyncio
async def test_the_others_are_called_at_their_url_with_max_tokens(wire, name, url):
    wire["reply"] = _chat("ok")
    cfg = {"ai_provider": name, f"{name}_api_key": "k", f"{name}_model": "m"}

    await p.complete(cfg, "", "ping", max_tokens=50)

    req = wire["requests"][0]
    assert req["url"] == url
    assert req["json"]["max_tokens"] >= 50
    assert req["json"]["messages"] == [{"role": "user", "content": "ping"}]


@pytest.mark.asyncio


async def test_a_reply_cut_off_at_the_budget_raises(wire):
    wire["reply"] = _chat("half an ans", finish="length")
    cfg = {"ai_provider": "mistral", "mistral_api_key": "k", "mistral_model": "m"}

    with pytest.raises(p.TruncatedResponseError):
        await p.complete(cfg, "", "ping", max_tokens=5)


@pytest.mark.asyncio


async def test_no_key_raises_without_calling_anybody(wire):
    with pytest.raises(RuntimeError):
        await p.complete({"ai_provider": "openai", "openai_model": "m"}, "", "x", 5)
    assert wire["requests"] == []


@pytest.mark.asyncio


async def test_vision_sends_the_images_as_data_urls(wire):
    wire["reply"] = _chat("a chart")
    cfg = {"ai_provider": "openai", "openai_api_key": "k", "openai_model": "m"}

    await p.complete_vision(cfg, "", "what is it", [b"\x89PNG\r\n\x1a\n...."], 100)

    parts = wire["requests"][0]["json"]["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "what is it"}
    assert parts[1]["image_url"]["url"].startswith("data:image/png;base64,")


@pytest.mark.asyncio


async def test_the_model_list_is_fetched_and_gemini_prefixes_are_dropped(wire):
    wire["reply"] = {"data": [{"id": "models/gemini-a"}, {"id": "gemini-b"}]}

    models = await p.fetch_available_models("gemini", "k")

    assert wire["requests"][0]["url"] == \
        "https://generativelanguage.googleapis.com/v1beta/openai/models"
    assert models == ["gemini-a", "gemini-b"]


@pytest.mark.asyncio


async def test_a_model_list_that_fails_falls_back_rather_than_raising(wire):
    wire["status"] = 401
    wire["reply"] = {"error": "bad key"}

    assert await p.fetch_available_models("openai", "bad") == \
        p.OPENAI_COMPATIBLE["openai"]["fallback"]


def test_ai_cfg_carries_every_providers_key_and_model():
    """claude_reviewer and the reversal tuner build the provider cfg by hand;
    one that only copies Claude's and DeepSeek's keys cannot reach OpenAI."""
    src = {"ai_provider": "openai", "openai_api_key": "k", "openai_model": "m",
           "unrelated": 1}
    cfg = p.ai_cfg(src.get)

    assert cfg["openai_api_key"] == "k" and cfg["openai_model"] == "m"
    assert cfg["ai_provider"] == "openai"
    assert "unrelated" not in cfg


def test_config_loads_a_key_and_model_for_every_registry_provider():
    """config imports nothing, so it repeats the registry's names. If the two
    drift, a provider can be chosen whose key config.yaml never loads."""
    from backend.src import config

    assert set(config.OPENAI_COMPATIBLE_PROVIDERS) == set(p.OPENAI_COMPATIBLE)
