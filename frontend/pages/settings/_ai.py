"""AI provider tab: the Claude and DeepSeek cards, model discovery and the
config push to the remote clients."""
from nicegui import ui

from backend.src.controllers import sync_controller as sync_ctl

from ._shared import cfg_module
# Current Claude model catalogue — update when Anthropic releases new versions
# claude-fable-5, claude-opus-4-8, claude-sonnet-4-6, claude-haiku-4-5-20251001
CLAUDE_MODELS = [
    "claude-fable-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-4-5-20251001",
]
# Legacy model IDs that may be stored in older config files → map to current ID.
# Prevents ValueError when ui.select receives a value not in CLAUDE_MODELS.
_CLAUDE_MODEL_ALIASES: dict[str, str] = {
    "claude-haiku-4-5":   "claude-haiku-4-5-20251001",
    "claude-sonnet-4-5":  "claude-sonnet-4-6",
    "claude-opus-4-5":    "claude-opus-4-8",
}
_CLAUDE_DEFAULT = "claude-sonnet-4-6"


def _resolve_claude_model(stored: str) -> str:
    """Return a valid CLAUDE_MODELS entry for *stored*, falling back to the default."""
    if stored in CLAUDE_MODELS:
        return stored
    if stored in _CLAUDE_MODEL_ALIASES:
        return _CLAUDE_MODEL_ALIASES[stored]
    return _CLAUDE_DEFAULT


async def _push_ai_config_to_vps(updates: dict) -> None:
    """Mirror an AI provider/model/key change to the paired VPS, if connected
    — best-effort, matching push_trade_closed()'s fire-and-forget pattern.
    Settings > AI has always been per-node (a separate config.yaml on each
    side, unlike the risk-settings sync); this is what makes "the provider I
    picked here" actually apply on both nodes instead of only the one whose
    UI you happened to save it on."""
    try:
        await sync_ctl.push_ai_config(updates)
    except Exception:
        pass


def _render_ai(engine):
    """AI provider settings: Claude / DeepSeek sub-tabs plus which one is active
    across the app (trade commentary, market analysis, channel strategy AI,
    breakout/test-signal review gates, the strategy builder)."""
    cfg = cfg_module.load_config()

    with ui.card().classes("w-full max-w-xl bg-gray-800 border border-purple-600 p-4 rounded-lg mb-4"):
        ui.label("AI Provider").classes("text-base font-bold text-yellow-300 mb-2")
        ui.label(
            "Whichever provider is selected here is used everywhere the app calls an "
            "AI model — trade commentary, market analysis, channel strategy selection, "
            "signal quality review, and the strategy builder."
        ).classes("text-xs text-gray-400 mb-3")
        provider_radio = ui.radio(
            {"claude": "Claude", "deepseek": "DeepSeek"},
            value=cfg.get("ai_provider", "claude") if cfg.get("ai_provider") in ("claude", "deepseek") else "claude",
        ).props("inline")

        async def _save_provider():
            cfg_module.save_config({"ai_provider": provider_radio.value})
            engine._cfg["ai_provider"] = provider_radio.value
            await _push_ai_config_to_vps({"ai_provider": provider_radio.value})
            ui.notify(f"AI provider set to {provider_radio.value.title()}", type="positive")

        ui.button("Save", icon="save", on_click=_save_provider).classes(
            "bg-blue-700 text-white text-xs px-3 py-1 mt-1"
        )

    with ui.tabs().classes("bg-gray-800") as ai_tabs:
        t_claude   = ui.tab("Claude")
        t_deepseek = ui.tab("DeepSeek")

    with ui.tab_panels(ai_tabs, value=t_claude).classes("bg-gray-900 p-0"):
        with ui.tab_panel(t_claude):
            _render_claude_card(engine)
        with ui.tab_panel(t_deepseek):
            _render_deepseek_card(engine)


def _render_claude_card(engine):
    from backend.src.controllers import ai_controller as ai_provider
    cfg = cfg_module.load_config()

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Claude Integration").classes("text-base font-bold text-yellow-300 mb-3")

        with ui.row().classes("w-full items-center gap-1"):
            api_key_in = ui.input(
                "Anthropic API Key", value=cfg.get("anthropic_api_key", ""), password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Required for Claude AI trade commentary. Get yours at console.anthropic.com."
            )

        _model_options = cfg.get("claude_models_cache") or CLAUDE_MODELS
        with ui.row().classes("w-full items-center gap-1"):
            claude_model_in = ui.select(
                _model_options,
                value=_resolve_claude_model(cfg.get("claude_model", _CLAUDE_DEFAULT))
                      if cfg.get("claude_model", _CLAUDE_DEFAULT) in _model_options
                      else (_model_options[0] if _model_options else _CLAUDE_DEFAULT),
                label="Claude Model",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Model used for trade commentary and signal analysis. "
                "The list refreshes daily from Anthropic's own model catalogue — "
                "use Refresh below to check right now."
            )

        refresh_lbl = ui.label(
            f"Model list last refreshed: "
            f"{_fmt_last_refresh(cfg.get('ai_models_last_refreshed', 0))}"
        ).classes("text-xs text-gray-500")

        async def _refresh_models():
            refresh_lbl.text = "Refreshing model list..."
            models = await ai_provider.fetch_available_models("claude", api_key_in.value)
            claude_model_in.set_options(models)
            now = __import__("time").time()
            cfg_module.save_config({"claude_models_cache": models, "ai_models_last_refreshed": now})
            engine._cfg["claude_models_cache"]      = models
            engine._cfg["ai_models_last_refreshed"] = now
            refresh_lbl.text = f"Model list last refreshed: {_fmt_last_refresh(now)}"

        test_result = ui.label("").classes("text-sm mt-1")

        async def test_anthropic():
            test_result.text = "Testing..."
            test_result.classes(replace="text-sm mt-1 text-gray-400")
            try:
                # Tests the key/model currently typed in the form — not yet
                # saved — by routing through the same ai_provider.complete()
                # abstraction Run Analysis/Strategy Builder use, via a
                # transient cfg dict, rather than a separate hardcoded
                # anthropic.Anthropic() call that could silently drift out of
                # sync with how the app actually calls Claude.
                _test_cfg = {
                    "ai_provider":       "claude",
                    "anthropic_api_key": api_key_in.value,
                    "claude_model":      claude_model_in.value,
                }
                await ai_provider.complete(_test_cfg, "", "ping", max_tokens=5, timeout=15)
                test_result.text = f"Connected — model: {claude_model_in.value}"
                test_result.classes(replace="text-sm mt-1 text-green-400")
            except Exception as e:
                test_result.text = f"Failed: {e}"
                test_result.classes(replace="text-sm mt-1 text-red-400")

        async def save_config():
            cfg_module.save_config({
                "anthropic_api_key": api_key_in.value,
                "claude_model":      claude_model_in.value,
            })
            engine._cfg["anthropic_api_key"] = api_key_in.value
            engine._cfg["claude_model"]      = claude_model_in.value
            # anthropic_api_key deliberately not pushed to the VPS — it may have
            # its own separately-provisioned Claude key/account; only the model
            # choice is synced, matching _AI_CONFIG_SYNC_KEYS in sync/server.py.
            await _push_ai_config_to_vps({"claude_model": claude_model_in.value})
            ui.notify("Config saved", type="positive")

        with ui.row().classes("gap-2 mt-3"):
            ui.button("Save Config", on_click=save_config).classes(
                "bg-blue-700 text-white px-4 py-2"
            )
            ui.button("Test Connection", on_click=test_anthropic).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
            ui.button("Refresh Models", icon="refresh", on_click=_refresh_models).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
        test_result


def _render_deepseek_card(engine):
    from backend.src.controllers import ai_controller as ai_provider
    cfg = cfg_module.load_config()

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("DeepSeek Integration").classes("text-base font-bold text-yellow-300 mb-3")

        with ui.row().classes("w-full items-center gap-1"):
            api_key_in = ui.input(
                "DeepSeek API Key", value=cfg.get("deepseek_api_key", ""), password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Required for DeepSeek trade commentary. Get yours at platform.deepseek.com."
            )

        _model_options = cfg.get("deepseek_models_cache") or ai_provider.FALLBACK_DEEPSEEK_MODELS
        _stored_model = cfg.get("deepseek_model", "")
        with ui.row().classes("w-full items-center gap-1"):
            deepseek_model_in = ui.select(
                _model_options,
                value=_stored_model if _stored_model in _model_options
                      else (_model_options[0] if _model_options else ""),
                label="DeepSeek Model",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The list refreshes daily from DeepSeek's own model catalogue — "
                "use Refresh below to check right now."
            )

        refresh_lbl = ui.label(
            f"Model list last refreshed: "
            f"{_fmt_last_refresh(cfg.get('ai_models_last_refreshed', 0))}"
        ).classes("text-xs text-gray-500")

        async def _refresh_models():
            refresh_lbl.text = "Refreshing model list..."
            models = await ai_provider.fetch_available_models("deepseek", api_key_in.value)
            deepseek_model_in.set_options(models)
            now = __import__("time").time()
            cfg_module.save_config({"deepseek_models_cache": models, "ai_models_last_refreshed": now})
            engine._cfg["deepseek_models_cache"]    = models
            engine._cfg["ai_models_last_refreshed"] = now
            refresh_lbl.text = f"Model list last refreshed: {_fmt_last_refresh(now)}"

        test_result = ui.label("").classes("text-sm mt-1")

        async def test_deepseek():
            test_result.text = "Testing..."
            test_result.classes(replace="text-sm mt-1 text-gray-400")
            try:
                test_cfg = {
                    "ai_provider": "deepseek",
                    "deepseek_api_key": api_key_in.value,
                    "deepseek_model": deepseek_model_in.value,
                }
                # Directive prompt (not bare "ping") — an open-ended prompt lets
                # the model write a full sentence and hit max_tokens before
                # finishing, even with thinking mode off; asking for one exact
                # word keeps this a real but small connectivity check.
                reply = await ai_provider.complete(
                    test_cfg, "", "Reply with just the word: pong", max_tokens=10, timeout=15
                )
                test_result.text = f"Connected — model: {deepseek_model_in.value}, reply: {reply[:40]!r}"
                test_result.classes(replace="text-sm mt-1 text-green-400")
            except Exception as e:
                test_result.text = f"Failed: {e}"
                test_result.classes(replace="text-sm mt-1 text-red-400")

        async def save_config():
            cfg_module.save_config({
                "deepseek_api_key": api_key_in.value,
                "deepseek_model":   deepseek_model_in.value,
            })
            engine._cfg["deepseek_api_key"] = api_key_in.value
            engine._cfg["deepseek_model"]   = deepseek_model_in.value
            # Unlike the Claude key above, the DeepSeek key IS synced to the VPS
            # — explicit user choice (2026-07-08), since the VPS had no DeepSeek
            # key of its own and syncing the provider choice alone would just
            # leave it unable to make any AI calls at all.
            await _push_ai_config_to_vps({
                "deepseek_api_key": api_key_in.value,
                "deepseek_model":   deepseek_model_in.value,
            })
            ui.notify("Config saved", type="positive")

        with ui.row().classes("gap-2 mt-3"):
            ui.button("Save Config", on_click=save_config).classes(
                "bg-blue-700 text-white px-4 py-2"
            )
            ui.button("Test Connection", on_click=test_deepseek).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
            ui.button("Refresh Models", icon="refresh", on_click=_refresh_models).classes(
                "bg-gray-600 text-white px-4 py-2"
            )
        test_result


def _fmt_last_refresh(ts: float) -> str:
    if not ts:
        return "never"
    from datetime import datetime as _dt
    return _dt.fromtimestamp(ts).strftime("%d %b %H:%M")
