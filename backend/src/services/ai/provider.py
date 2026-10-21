"""Unified LLM provider abstraction — lets the rest of the app call a single
`complete()` function without caring which provider is configured. Selected
via cfg["ai_provider"] (one of PROVIDERS), set from Settings > AI.

Claude and DeepSeek have their own adapters. Everything in OPENAI_COMPATIBLE
(OpenAI, Google Gemini, xAI, Mistral, OpenRouter -- owner, 2026-10-21) speaks
the OpenAI chat-completions protocol and shares one: adding another such
provider is one registry entry. Each keeps `<name>_api_key`, `<name>_model`
and `<name>_models_cache` in config.yaml.

Only the "get text back from a system+user prompt" step is unified here —
each caller in claude_ai.py keeps its own JSON-schema prompt, response
parsing, and fallback dict, since those are bespoke per use case.
"""

import json
import logging
from typing import Optional

from backend.src.config import is_debug as _is_debug

log = logging.getLogger(__name__)

try:
    import anthropic as _anthropic_module
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _anthropic_module = None
    _ANTHROPIC_AVAILABLE = False

import httpx

_DEEPSEEK_BASE_URL = "https://api.deepseek.com"

# Safe, small fallback lists used only if a live models.list()/GET /models call
# fails (no key yet, network down, provider outage) — the daily refresh loop
# (SimulationEngine._ai_model_refresh_loop) keeps the real cached list current
# under normal operation, this is just so the Settings dropdown is never empty.
FALLBACK_CLAUDE_MODELS = [
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]
FALLBACK_DEEPSEEK_MODELS = [
    "deepseek-v4-pro",
    "deepseek-v4-flash",
]


# Base URLs checked against each vendor's docs on 2026-10-21. `token_param`:
# OpenAI's reasoning models refuse `max_tokens`. `headroom` is added to every
# budget for providers whose models may reason before answering, because
# reasoning spends the same budget and the call sites' budgets (5-2000) were
# sized for answers alone -- DeepSeek's thinking switch exists for the same
# reason. A cut-off reply still raises TruncatedResponseError.
# Fallback lists are deliberately empty where no current model id could be
# confirmed: the Settings tab then takes a typed id, and "Refresh models" asks
# the vendor. A guessed id would be a dropdown entry that fails on first use.
OPENAI_COMPATIBLE: dict[str, dict] = {
    "openai": {"label": "OpenAI", "base_url": "https://api.openai.com/v1",
               "token_param": "max_completion_tokens", "headroom": 2048,
               "fallback": []},
    "gemini": {"label": "Google Gemini",
               "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
               "token_param": "max_tokens", "headroom": 2048,
               "fallback": ["gemini-3.8-flash"]},
    "xai": {"label": "xAI (Grok)", "base_url": "https://api.x.ai/v1",
            "token_param": "max_tokens", "headroom": 2048, "fallback": []},
    "mistral": {"label": "Mistral", "base_url": "https://api.mistral.ai/v1",
                "token_param": "max_tokens", "headroom": 0, "fallback": []},
    "openrouter": {"label": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                   "token_param": "max_tokens", "headroom": 2048, "fallback": []},
}

PROVIDERS: tuple[str, ...] = ("claude", "deepseek", *OPENAI_COMPATIBLE)
LABELS: dict[str, str] = {"claude": "Claude", "deepseek": "DeepSeek",
                          **{k: v["label"] for k, v in OPENAI_COMPATIBLE.items()}}


def key_field(provider: str) -> str:
    """Claude's key is Anthropic's own name for it; every other is `<name>_api_key`."""
    return "anthropic_api_key" if provider == "claude" else f"{provider}_api_key"


def model_field(provider: str) -> str:
    return f"{provider}_model"


def fallback_models(provider: str) -> list[str]:
    if provider == "claude":
        return FALLBACK_CLAUDE_MODELS
    if provider == "deepseek":
        return FALLBACK_DEEPSEEK_MODELS
    return list(OPENAI_COMPATIBLE.get(provider, {}).get("fallback", []))


def ai_cfg(get) -> dict:
    """The AI slice of a config, for call sites that build one by hand.

    `get` is a `cfg.get`-shaped callable. Copies every provider's key and
    model, so a caller cannot reach Claude and DeepSeek only.
    """
    out = {"ai_provider": get("ai_provider", "claude")}
    for name in PROVIDERS:
        out[key_field(name)] = get(key_field(name), "")
        out[model_field(name)] = get(model_field(name), "")
    return out


class TruncatedResponseError(Exception):
    """Raised when the provider truncated its reply at max_tokens before
    finishing — callers that need a complete JSON object should treat this
    the same as a failure rather than trying to parse a cut-off response."""


def is_configured(cfg: dict) -> bool:
    """Whether the currently-selected provider has what it needs to be called."""
    provider = cfg.get("ai_provider", "claude")
    if provider == "deepseek" or provider in OPENAI_COMPATIBLE:
        return bool(cfg.get(key_field(provider)))
    return _ANTHROPIC_AVAILABLE and bool(cfg.get("anthropic_api_key"))


def active_model(cfg: dict) -> str:
    """The model id the selected provider would actually send. Asks nothing.

    Branches on ai_provider exactly as complete() does, including the fall-
    through to Claude when ai_provider is absent -- a label that fell a
    different way would describe a different call than the one that was made.
    It reads only the selected provider's model key: `claude_model` carries a
    config default that is present whether or not Claude is selected, so
    `claude_model or deepseek_model` named Claude above every DeepSeek answer.
    """
    provider = cfg.get("ai_provider", "claude")
    if provider == "deepseek" or provider in OPENAI_COMPATIBLE:
        return str(cfg.get(model_field(provider)) or "")
    return str(cfg.get("claude_model") or "")


async def complete(cfg: dict, system: str, prompt: str, max_tokens: int, timeout: int = 30) -> str:
    """Return the raw text reply from the configured provider. system may be
    "" for callers with no separate system prompt. Raises on any failure
    (missing key, network error, timeout, truncation) — callers already wrap
    these calls in their own try/except with a fallback dict."""
    if _is_debug():
        # Deterministic canned reply — no SDK client, no network. Tagged so
        # it can never be mistaken for a real analysis.
        return ("[debug-canned] Simulated AI response (debug mode — no "
                "provider was called). Prompt head: " + prompt[:120])
    provider = cfg.get("ai_provider", "claude")
    if provider == "deepseek":
        return await _complete_deepseek(cfg, system, prompt, max_tokens, timeout)
    if provider in OPENAI_COMPATIBLE:
        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        return await _complete_openai_compatible(cfg, provider, messages, max_tokens, timeout)
    return await _complete_claude(cfg, system, prompt, max_tokens, timeout)


def _sniff_image_mime(data: bytes) -> str:
    """Guess an image's MIME type from its header bytes. Telethon's
    download_media() doesn't report the original format, and Telegram photos
    are usually JPEG but not guaranteed (stickers/some clients send PNG/WEBP)."""
    if data[:2] == b"\xff\xd8":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return "image/jpeg"  # best-effort fallback


async def complete_vision(cfg: dict, system: str, prompt: str, images: list[bytes],
                           max_tokens: int, timeout: int = 60) -> str:
    """Like complete(), but attaches one or more images to the user message.
    Routes by cfg["ai_provider"] exactly like complete() — whichever provider
    is selected in Settings > AI is the one that gets the images. Callers
    needing a fallback for a provider/model that can't actually handle
    images should catch the resulting error and retry with plain complete()
    on the SAME provider, rather than this function silently switching
    provider on the caller's behalf."""
    if _is_debug():
        return ("[debug-canned] Simulated AI vision response (debug mode — "
                f"{len(images)} image(s) not analysed).")
    provider = cfg.get("ai_provider", "claude")
    if provider == "deepseek":
        return await _complete_vision_deepseek(cfg, system, prompt, images, max_tokens, timeout)
    if provider in OPENAI_COMPATIBLE:
        return await _complete_openai_compatible(
            cfg, provider, _vision_messages(system, prompt, images), max_tokens, timeout)
    return await _complete_vision_claude(cfg, system, prompt, images, max_tokens, timeout)


async def _complete_vision_claude(cfg: dict, system: str, prompt: str, images: list[bytes],
                                   max_tokens: int, timeout: int) -> str:
    api_key = cfg.get("anthropic_api_key", "")
    model   = cfg.get("claude_model", "")
    if not _ANTHROPIC_AVAILABLE or not api_key:
        raise RuntimeError("Claude not configured (missing API key or anthropic package)")
    import base64
    content: list[dict] = []
    for img in images:
        content.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": _sniff_image_mime(img),
                "data": base64.b64encode(img).decode(),
            },
        })
    content.append({"type": "text", "text": prompt})
    client = _anthropic_module.AsyncAnthropic(api_key=api_key)
    kwargs = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": content}], "timeout": timeout}
    if system:
        kwargs["system"] = system
    resp = await client.messages.create(**kwargs)
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise TruncatedResponseError("Claude response truncated at max_tokens")
    return resp.content[0].text.strip()


def _vision_messages(system: str, prompt: str, images: list[bytes]) -> list[dict]:
    """The OpenAI-protocol user message with images inlined as data URLs."""
    import base64
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        mime = _sniff_image_mime(img)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(img).decode()}"},
        })
    return ([{"role": "system", "content": system}] if system else []) + \
           [{"role": "user", "content": content}]


async def _complete_openai_compatible(cfg: dict, provider: str, messages: list[dict],
                                      max_tokens: int, timeout: int) -> str:
    spec = OPENAI_COMPATIBLE[provider]
    api_key = cfg.get(key_field(provider), "")
    model = cfg.get(model_field(provider), "")
    if not api_key:
        raise RuntimeError(f"{spec['label']} not configured (missing API key)")
    if not model:
        raise RuntimeError(f"{spec['label']} has no model selected (Settings > AI)")
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{spec['base_url']}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={"model": model, "messages": messages,
                  spec["token_param"]: max_tokens + spec["headroom"]},
        )
        resp.raise_for_status()
        data = resp.json()
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise TruncatedResponseError(f"{spec['label']} response truncated at max_tokens")
    return (choice["message"].get("content") or "").strip()


async def _complete_vision_deepseek(cfg: dict, system: str, prompt: str, images: list[bytes],
                                     max_tokens: int, timeout: int) -> str:
    api_key = cfg.get("deepseek_api_key", "")
    model   = cfg.get("deepseek_model", "")
    if not api_key:
        raise RuntimeError("DeepSeek not configured (missing API key)")
    import base64
    content: list[dict] = [{"type": "text", "text": prompt}]
    for img in images:
        mime = _sniff_image_mime(img)
        content.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{base64.b64encode(img).decode()}"},
        })
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": content}]
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{_DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model, "max_tokens": max_tokens, "messages": messages,
                "thinking": {"type": "disabled"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise TruncatedResponseError("DeepSeek response truncated at max_tokens")
    return choice["message"]["content"].strip()


async def _complete_claude(cfg: dict, system: str, prompt: str, max_tokens: int, timeout: int) -> str:
    api_key = cfg.get("anthropic_api_key", "")
    model   = cfg.get("claude_model", "")
    if not _ANTHROPIC_AVAILABLE or not api_key:
        raise RuntimeError("Claude not configured (missing API key or anthropic package)")
    client = _anthropic_module.AsyncAnthropic(api_key=api_key)
    kwargs = {"model": model, "max_tokens": max_tokens,
              "messages": [{"role": "user", "content": prompt}], "timeout": timeout}
    if system:
        kwargs["system"] = system
    resp = await client.messages.create(**kwargs)
    if getattr(resp, "stop_reason", None) == "max_tokens":
        raise TruncatedResponseError("Claude response truncated at max_tokens")
    return resp.content[0].text.strip()


async def _complete_deepseek(cfg: dict, system: str, prompt: str, max_tokens: int, timeout: int) -> str:
    api_key = cfg.get("deepseek_api_key", "")
    model   = cfg.get("deepseek_model", "")
    if not api_key:
        raise RuntimeError("DeepSeek not configured (missing API key)")
    messages = ([{"role": "system", "content": system}] if system else []) + \
               [{"role": "user", "content": prompt}]
    async with httpx.AsyncClient(timeout=timeout) as client:
        resp = await client.post(
            f"{_DEEPSEEK_BASE_URL}/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "max_tokens": max_tokens,
                "messages": messages,
                # Both deepseek-v4-flash and deepseek-v4-pro default to thinking
                # mode, which burns the max_tokens budget on hidden
                # reasoning_content before ever writing the real answer —
                # confirmed live: max_tokens=50 with thinking on produced an
                # empty content field, 100% consumed by reasoning. None of this
                # app's call sites (structured JSON extraction, short
                # commentary) benefit from extended reasoning, and their
                # existing token budgets (200-2000, copied from Claude usage)
                # assume the whole budget goes to the real answer.
                "thinking": {"type": "disabled"},
            },
        )
        resp.raise_for_status()
        data = resp.json()
    choice = data["choices"][0]
    if choice.get("finish_reason") == "length":
        raise TruncatedResponseError("DeepSeek response truncated at max_tokens")
    return choice["message"]["content"].strip()


async def fetch_available_models(provider: str, api_key: str) -> list[str]:
    """Live model-list lookup for the Settings > AI dropdowns and the daily
    refresh loop. Falls back to a small static list on any failure so the
    dropdown is never left empty."""
    try:
        if provider == "deepseek":
            return await _fetch_deepseek_models(api_key)
        if provider in OPENAI_COMPATIBLE:
            return await _fetch_openai_compatible_models(provider, api_key)
        return await _fetch_claude_models(api_key)
    except Exception as e:
        log.warning("fetch_available_models(%s) failed, using fallback: %s", provider, e)
        return fallback_models(provider)


async def _fetch_openai_compatible_models(provider: str, api_key: str) -> list[str]:
    if not api_key:
        return fallback_models(provider)
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{OPENAI_COMPATIBLE[provider]['base_url']}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    # Gemini's list names models "models/<id>"; the chat call wants "<id>".
    ids = [str(m["id"]).removeprefix("models/") for m in data.get("data", []) if m.get("id")]
    return ids or fallback_models(provider)


async def _fetch_claude_models(api_key: str) -> list[str]:
    if not _ANTHROPIC_AVAILABLE or not api_key:
        return FALLBACK_CLAUDE_MODELS
    client = _anthropic_module.AsyncAnthropic(api_key=api_key)
    page = await client.models.list(limit=100)
    ids = [m.id for m in page.data]
    return ids or FALLBACK_CLAUDE_MODELS


async def _fetch_deepseek_models(api_key: str) -> list[str]:
    if not api_key:
        return FALLBACK_DEEPSEEK_MODELS
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{_DEEPSEEK_BASE_URL}/models",
            headers={"Authorization": f"Bearer {api_key}"},
        )
        resp.raise_for_status()
        data = resp.json()
    ids = [m["id"] for m in data.get("data", [])]
    return ids or FALLBACK_DEEPSEEK_MODELS
