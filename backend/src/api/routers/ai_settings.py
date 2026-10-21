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
from pydantic import BaseModel, ConfigDict

from backend.src.api.errors import Refusal
from backend.src.controllers import ai_controller as ai_ctl
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import sync_controller as sync_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/ai/settings", tags=["ai-settings"])

PROVIDERS = ai_ctl.PROVIDERS


class AiSettingsWrite(BaseModel):
    """`provider`, plus any `<provider>_model` and `<key field>` for a known
    provider. Anything else is dropped before it can reach config.yaml."""
    model_config = ConfigDict(extra="allow")

    provider: str | None = None
    # Blank means "keep the stored one", for the same reason as every other
    # secret here: the field is empty on screen whether or not one is set, so
    # passing it through would erase a working key on every unrelated edit.


_WRITABLE = {f(p) for p in PROVIDERS for f in (ai_ctl.key_field, ai_ctl.model_field)}


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
    info = {
        p: {
            "label": ai_ctl.LABELS.get(p, p),
            "model": cfg.get(ai_ctl.model_field(p)) or "",
            "key_set": bool(cfg.get(ai_ctl.key_field(p))),
            # Cached from the last successful refresh. Offered so the screen
            # has something to choose from before anybody presses Refresh.
            "models": list(cfg.get(f"{p}_models_cache") or ai_ctl.fallback_models(p)),
        }
        for p in PROVIDERS
    }
    return {
        "provider": cfg.get("ai_provider") or "claude",
        "providers": list(PROVIDERS),
        "provider_info": info,
        # The flat Claude/DeepSeek fields predate provider_info and are kept
        # for anything still reading them.
        "claude_model": info["claude"]["model"],
        "deepseek_model": info["deepseek"]["model"],
        "anthropic_api_key_set": info["claude"]["key_set"],
        "deepseek_api_key_set": info["deepseek"]["key_set"],
        "claude_models": list(cfg.get("claude_models_cache") or []),
        "deepseek_models": info["deepseek"]["models"],
        "configured": ai_ctl.is_configured(cfg),
    }


@router.put("")
async def write(body: AiSettingsWrite) -> dict:
    """Save what was given, keep what was not, and tell the paired node."""
    given = {k: v for k, v in body.model_dump().items()
             if k in _WRITABLE and isinstance(v, str) and v.strip()}
    if body.provider is not None:
        # `ai_provider` is the key every caller reads. Until 2026-10-21 this
        # saved `provider`, which nothing read, so the dropdown changed nothing.
        given["ai_provider"] = _require_known(body.provider)
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
        cfg[ai_ctl.key_field(provider)] = body.api_key
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
    key = body.api_key or cfg.get(ai_ctl.key_field(provider)) or ""
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
