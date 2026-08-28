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
from backend.src.services.ai import provider as _provider

__all__ = [
    "is_configured",
    "complete",
    "fetch_available_models",
    "FALLBACK_DEEPSEEK_MODELS",
    "request_commentary",
    "request_market_analysis",
]

# Offered in Settings when the DeepSeek model list cannot be fetched live.
FALLBACK_DEEPSEEK_MODELS = _provider.FALLBACK_DEEPSEEK_MODELS


def is_configured(cfg: dict) -> bool:
    """Whether a provider and key are set. Makes no request."""
    return _provider.is_configured(cfg)


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
