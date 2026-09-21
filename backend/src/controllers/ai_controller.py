"""The AI provider, as the UI calls it.

Four names off the provider (is_configured, complete, fetch_available_models
and the DeepSeek fallback list) and two off claude_ai (commentary and market
analysis). Six between them, across five pages.

Forwards unchanged. The provider decides which vendor answers and how to
retry; this only means a page cannot be rewired by a change to that module's
signature without this file noticing first.

complete() and the two claude_ai calls REACH AN EXTERNAL API and cost money
per call. That is what they did before this file existed -- routed, not
altered -- but a controller whose functions are mostly inert should say which
ones are not. is_configured() and FALLBACK_DEEPSEEK_MODELS touch nothing.
"""
from __future__ import annotations

from backend.src.services.ai import claude_ai as _claude
from backend.src.services.ai import market_research as _research
from backend.src.services.ai import provider as _provider

__all__ = [
    "is_configured",
    "active_model",
    "complete",
    "fetch_available_models",
    "FALLBACK_DEEPSEEK_MODELS",
    "request_commentary",
    "request_market_analysis",
    "run_market_research",
    "last_market_research",
]

# Offered in Settings when the DeepSeek model list cannot be fetched live.
FALLBACK_DEEPSEEK_MODELS = _provider.FALLBACK_DEEPSEEK_MODELS


def is_configured(cfg: dict) -> bool:
    """Whether a provider and key are set. Makes no request."""
    return _provider.is_configured(cfg)


def active_model(cfg: dict) -> str:
    """Which model the selected provider would send. Makes no request."""
    return _provider.active_model(cfg)


async def complete(cfg: dict, system: str, prompt: str,
                   max_tokens: int, timeout: int = 30) -> str:
    """Send a prompt to the configured provider. Billable."""
    return await _provider.complete(cfg, system, prompt, max_tokens, timeout=timeout)


async def fetch_available_models(provider: str, api_key: str) -> list[str]:
    """Ask the vendor which models the key can use. Network call."""
    return await _provider.fetch_available_models(provider, api_key)


async def request_commentary(*args, **kwargs):
    """Trade commentary. Billable."""
    return await _claude.request_commentary(*args, **kwargs)


async def request_market_analysis(*args, **kwargs):
    """Market analysis. Billable."""
    return await _claude.request_market_analysis(*args, **kwargs)


async def run_market_research(engine, **kwargs):
    """The AI Analysis tab's whole answer, in one model call. **Billable.**

    The gathering -- tick, candles, signals, performance, the strategy
    catalogue -- belongs to the service; this only routes to it.
    """
    return await _research.run_research(engine, **kwargs)


def last_market_research() -> dict:
    """The analysis this install last ran. Asks no model and costs nothing."""
    return _research.last_research()
