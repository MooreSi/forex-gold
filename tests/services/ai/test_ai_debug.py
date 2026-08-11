"""Canned AI responses in debug mode (stage2 phase5/020).

In debug the provider returns deterministic canned text tagged
[debug-canned] and constructs no SDK client; the daily model-refresh loop
does not run. Debug off is byte-identical (negative controls).

No test here can reach Anthropic/DeepSeek: providers are patched.
"""
from __future__ import annotations

import asyncio
import inspect
from unittest.mock import AsyncMock, patch

from backend.src.services.ai import model_refresh_loop, provider


def test_complete_returns_canned_in_debug():
    with patch.object(provider, "_is_debug", return_value=True), \
         patch.object(provider, "_complete_claude", new=AsyncMock()) as claude, \
         patch.object(provider, "_complete_deepseek", new=AsyncMock()) as deepseek:
        out = asyncio.run(provider.complete({"ai_provider": "claude"}, "sys", "prompt", 100))
    assert "[debug-canned]" in out
    claude.assert_not_awaited()
    deepseek.assert_not_awaited()


def test_complete_vision_returns_canned_in_debug():
    with patch.object(provider, "_is_debug", return_value=True), \
         patch.object(provider, "_complete_vision_claude", new=AsyncMock()) as vis:
        out = asyncio.run(provider.complete_vision({}, "sys", "prompt", [b"img"], 100))
    assert "[debug-canned]" in out
    vis.assert_not_awaited()


def test_complete_routes_normally_when_debug_off():
    """Negative control: debug off reaches the provider implementation."""
    with patch.object(provider, "_is_debug", return_value=False), \
         patch.object(provider, "_complete_claude",
                      new=AsyncMock(return_value="real")) as claude:
        out = asyncio.run(provider.complete({"ai_provider": "claude"}, "s", "p", 10))
    assert out == "real"
    claude.assert_awaited_once()


def test_refresh_loop_not_started_in_debug():
    """The loop returns before its first sleep in debug — asyncio.run
    completing at all is the proof (undebugged it sleeps 60s first)."""
    with patch.object(model_refresh_loop, "_is_debug", return_value=True):
        asyncio.run(model_refresh_loop.ai_model_refresh_loop({}, lambda: True))
    src = inspect.getsource(model_refresh_loop.ai_model_refresh_loop)
    assert src.index("_is_debug") < src.index("asyncio.sleep"), \
        "the debug guard must sit before the first sleep"


def test_rss_headlines_canned_in_debug():
    from backend.src.services.ai import claude_ai

    with patch.object(claude_ai, "_is_debug", return_value=True):
        headlines = asyncio.run(claude_ai._fetch_gold_news())
    assert headlines
    assert any("gold" in h.lower() for h in headlines)
    # httpx is imported inside the function, so the transport-untouched
    # proof is structural: the guard must precede the import.
    src = inspect.getsource(claude_ai._fetch_gold_news)
    assert src.index("_is_debug") < src.index("import httpx")
