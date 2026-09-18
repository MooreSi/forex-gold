"""Settings > AI: which model answers, and the key that lets it.

Restored 2026-09-18. The NiceGUI Settings page had an AI tab and the React port
dropped it, so between then and now **there was no way to enter an API key from
the dashboard** — every AI feature in the app (the Analysis tab, trade
commentary, the channel-strategy recommendations, the reversal tuner) was
unreachable on a fresh install unless somebody edited `config.yaml` by hand.

Three things this layer is careful about:

* **Keys are write-only.** `GET` reports whether one is stored, never what it
  is, like every other credential here.
* **Testing a key costs money.** The ping is five tokens and says so, and it
  tests the key that was TYPED rather than the one that is saved — otherwise
  "test before you save" is impossible and the operator saves an unverified
  key and finds out later.
* **The config is pushed to the paired node.** Settings > AI has always been
  per-node — a separate `config.yaml` on each side, unlike the risk settings,
  which sync. Without the push, "the provider I picked" applies only on
  whichever machine's dashboard happened to be open. Best-effort: a failed push
  must not lose the local save.
"""
from __future__ import annotations

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import ai_controller as ai_ctl
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import sync_controller as sync_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/settings", tags=["ai-settings"])

PROVIDERS = ("claude", "deepseek")

# Keys are `<provider>_api_key` except Claude's, which is Anthropic's own name
# for it and is what every other part of this app already reads.
KEY_FIELD = {"claude": "anthropic_api_key", "deepseek": "deepseek_api_key"}
MODEL_FIELD = {"claude": "claude_model", "deepseek": "deepseek_model"}


class AiSettingsWrite(BaseModel):
    provider: str | None = None
    claude_model: str | None = None
    deepseek_model: str | None = None
    # Blank means "keep the stored one", for the same reason as every other
    # secret here: the field is empty on screen whether or not one is set, so
    # passing it through would erase a working key on every unrelated edit.
    anthropic_api_key: str | None = None
    deepseek_api_key: str | None = None


class ProviderRef(BaseModel):
    provider: str
    # An unsaved key, so a key can be tested before it is committed.
    api_key: str = ""


def _require_known(provider: str) -> str:
    if provider not in PROVIDERS:
        raise Refusal(f"Unknown AI provider {provider!r}. "
                      f"Known: {', '.join(PROVIDERS)}.", status_code=400)
    return provider


@router.get("")
async def read() -> dict:
    """The provider, the models, and whether each key is stored."""
    cfg = settings_ctl.load_config() or {}
    return {
        "provider": cfg.get("ai_provider") or "claude",
        "providers": list(PROVIDERS),
        "claude_model": cfg.get("claude_model") or "",
        "deepseek_model": cfg.get("deepseek_model") or "",
        "anthropic_api_key_set": bool(cfg.get("anthropic_api_key")),
        "deepseek_api_key_set": bool(cfg.get("deepseek_api_key")),
        # Cached from the last successful refresh. Offered so the screen has
        # something to choose from before anybody presses Refresh.
        "claude_models": list(cfg.get("claude_models_cache") or []),
        "deepseek_models": list(cfg.get("deepseek_models_cache")
                                or ai_ctl.FALLBACK_DEEPSEEK_MODELS),
        "configured": ai_ctl.is_configured(cfg),
    }


@router.put("")
async def write(body: AiSettingsWrite) -> dict:
    """Save what was given, keep what was not, and tell the paired node."""
    given = {k: v for k, v in body.model_dump().items() if v not in (None, "")}
    if body.provider is not None:
        _require_known(body.provider)
    if not given:
        raise Refusal("Nothing to save.")

    settings_ctl.save_config(given)

    # Best-effort, and deliberately after the local save: the paired node not
    # hearing about it must not cost the setting on this one.
    try:
        await sync_ctl.push_ai_config(given)
    except Exception as exc:
        log.info("[ai-settings] could not push to the paired node: %s", exc)

    return await read()


@router.post("/test")
async def test(body: ProviderRef) -> dict:
    """Send five tokens to the provider to prove the key works. **Billable.**

    Uses the typed key when there is one, so a key can be checked before it is
    saved. Without that, "test before you save" is impossible and the operator
    commits an unverified key and discovers it when an analysis fails hours
    later.
    """
    provider = _require_known(body.provider)
    cfg = dict(settings_ctl.load_config() or {})
    cfg["ai_provider"] = provider
    if body.api_key:
        cfg[KEY_FIELD[provider]] = body.api_key
    if not ai_ctl.is_configured(cfg):
        raise Refusal(f"No API key for {provider}. Enter one and try again.")

    try:
        await ai_ctl.complete(cfg, "", "ping", max_tokens=5, timeout=15)
    except Exception as exc:
        raise Refusal(f"{provider} did not accept it: {exc}") from exc
    return {"provider": provider, "ok": True, "billable": True,
            "note": f"{provider} answered."}


@router.post("/models")
async def refresh_models(body: ProviderRef) -> dict:
    """Ask the vendor which models this key may use, and cache the answer.

    Cached because it is a network call on a screen the operator opens to type
    a key: an empty dropdown while a request is in flight reads as "this
    provider has no models".
    """
    provider = _require_known(body.provider)
    cfg = dict(settings_ctl.load_config() or {})
    key = body.api_key or cfg.get(KEY_FIELD[provider]) or ""
    if not key:
        raise Refusal(f"No API key for {provider}. Enter one and try again.")

    try:
        models = await ai_ctl.fetch_available_models(provider, key)
    except Exception as exc:
        raise Refusal(f"Could not fetch the model list: {exc}") from exc
    if not models:
        raise Refusal(f"{provider} returned no models for that key.")

    settings_ctl.save_config({f"{provider}_models_cache": list(models)})
    return {"provider": provider, "models": list(models)}
