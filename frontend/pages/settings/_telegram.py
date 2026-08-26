"""Telegram tab: bot token, chat id and the alert routing toggles."""
from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl

from ._shared import cfg_module


def _render_tg_bot():
    tg_cfg = settings_ctl.get_telegram_config()
    cfg    = cfg_module.load()

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-6 rounded-lg"):
        ui.label("Telegram Bot Alerts").classes("text-lg font-bold text-yellow-300 mb-4")
        ui.label(
            "These settings control the bot that SENDS trade notifications."
        ).classes("text-sm text-gray-400")

        with ui.row().classes("w-full items-center gap-1"):
            bot_token = ui.input(
                "Bot Token", value=tg_cfg.get("bot_token_enc", "") or "", password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Create a bot via @BotFather on Telegram. "
                "Token format: 1234567890:ABCDEF..."
            )

        with ui.row().classes("w-full items-center gap-1"):
            chat_id = ui.input("Chat ID", value=tg_cfg.get("chat_id", "") or "").classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The Telegram chat ID where alerts will be sent. "
                "Get this from @userinfobot or @RawDataBot."
            )

        with ui.row().classes("items-center gap-1"):
            enabled = ui.checkbox(
                "Enable Telegram alerts", value=bool(tg_cfg.get("enabled", 0))
            )
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "When enabled, trade opens, closes, and TP hits are sent to the chat ID above."
            )

        def save_tg():
            settings_ctl.save_telegram_config(bot_token.value, chat_id.value, enabled.value)
            ui.notify("Telegram config saved", type="positive")

        async def test_tg():
            import backend.src.services.telegram.alerts as alerts
            ok = await alerts.send_message(
                "*FOREX Trader — Test Alert*\nTelegram alerts are working.",
                event_type="test",
            )
            ui.notify(
                "Sent!" if ok else "Failed — check bot token and chat ID",
                type="positive" if ok else "negative",
            )

        with ui.row().classes("gap-2 mt-3"):
            ui.button("Save", on_click=save_tg).classes("bg-blue-700 text-white px-4 py-2")
            ui.button("Test Alert", on_click=test_tg).classes(
                "bg-gray-600 text-white px-4 py-2"
            )

    with ui.card().classes("w-full max-w-xl bg-gray-800 p-4 rounded-lg mt-4"):
        ui.label("Telegram Reader (Telethon)").classes("font-semibold text-yellow-300 mb-2")
        ui.label(
            "API credentials for reading signals. Change in config.yaml and restart."
        ).classes("text-sm text-gray-400")
        ui.label(
            f"API ID set:   {'Yes' if cfg.get('telegram_api_id') else 'No'}"
        ).classes("text-sm text-gray-300")
        ui.label(
            f"API Hash set: {'Yes' if cfg.get('telegram_api_hash') else 'No'}"
        ).classes("text-sm text-gray-300")
        ui.label(
            f"Phone set:    {'Yes' if cfg.get('telegram_phone') else 'No'}"
        ).classes("text-sm text-gray-300")

        with ui.row().classes("w-full items-center gap-1"):
            tg_api_id = ui.input(
                "API ID", value=str(cfg.get("telegram_api_id", "") or "")
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "From my.telegram.org/apps — your Telegram API application ID"
            )

        with ui.row().classes("w-full items-center gap-1"):
            tg_api_hash = ui.input(
                "API Hash", value=str(cfg.get("telegram_api_hash", "") or ""), password=True
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "From my.telegram.org/apps — your Telegram API application hash"
            )

        with ui.row().classes("w-full items-center gap-1"):
            tg_phone = ui.input(
                "Phone", value=str(cfg.get("telegram_phone", "") or "")
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Your Telegram account phone number in international format: +441234567890"
            )

        def save_tg_reader():
            cfg_module.save_to_yaml({
                "telegram_api_id":   tg_api_id.value,
                "telegram_api_hash": tg_api_hash.value,
                "telegram_phone":    tg_phone.value,
            })
            ui.notify("Saved to config.yaml — restart app for changes to take effect", type="info")

        ui.button("Save Reader Credentials", on_click=save_tg_reader).classes(
            "bg-blue-700 text-white mt-2 px-4 py-2"
        )
