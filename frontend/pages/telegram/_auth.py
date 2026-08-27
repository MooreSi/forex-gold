"""Telegram authentication: the login-code steps and the connected view."""
import asyncio
from typing import Callable

from nicegui import ui

from backend.src.controllers import telegram_controller as tg_controller

from ._feed import (
    _render_pending_question,
    _render_slot_feed,
    _render_stored_messages,
)


def _render_send_code_step(reader, on_done: Callable):
    from backend.src.controllers import settings_controller as cfg_module
    cfg = cfg_module.load_config()

    ui.label("Step 1: Send login code").classes("text-sm font-semibold text-gray-300 mb-2")
    api_id   = ui.input("API ID",   value=str(cfg.get("telegram_api_id", "") or "")).classes("w-full")
    api_hash = ui.input("API Hash", value=str(cfg.get("telegram_api_hash", "") or ""),
                         password=True).classes("w-full")
    phone    = ui.input("Phone number (+441234567890)",
                         value=str(cfg.get("telegram_phone", "") or "")).classes("w-full")
    err_lbl  = ui.label("").classes("text-red-300 text-sm")

    async def send():
        err_lbl.text = ""
        api_id_val   = int(api_id.value) if str(api_id.value).strip().isdigit() else None
        result = await reader.send_code(
            phone.value,
            api_id_override=api_id_val,
            api_hash_override=api_hash.value.strip() or None,
        )
        if "error" in result:
            err_lbl.text = result["error"]
        else:
            ui.notify("Code sent — check your Telegram app", type="positive")
            on_done()

    ui.button("Send Code", on_click=send).classes("bg-blue-700 text-white mt-3 px-4 py-2 w-full")
    ui.label("Get API credentials from https://my.telegram.org/apps").classes("text-sm text-gray-400")


def _render_verify_code_step(reader, on_done: Callable):
    ui.label("Step 2: Enter login code").classes("text-sm font-semibold text-gray-300 mb-2")
    code    = ui.input("Telegram login code").classes("w-full")
    err_lbl = ui.label("").classes("text-red-300 text-sm")

    async def verify():
        err_lbl.text = ""
        result = await reader.verify_code(code.value)
        if "error" in result:
            err_lbl.text = result["error"]
        elif result.get("auth_state") == "connected":
            ui.notify("Authenticated!", type="positive")
            on_done()
        elif result.get("auth_state") == "awaiting_2fa":
            ui.notify("2FA required — enter your password", type="info")
            on_done()

    ui.button("Verify Code", on_click=verify).classes("bg-blue-700 text-white mt-3 px-4 py-2 w-full")


def _render_verify_2fa_step(reader, on_done: Callable):
    ui.label("Step 3: Two-factor authentication password").classes(
        "text-sm font-semibold text-gray-300 mb-2"
    )
    password = ui.input("2FA Password", password=True).classes("w-full")
    err_lbl  = ui.label("").classes("text-red-300 text-sm")

    async def verify():
        err_lbl.text = ""
        result = await reader.verify_2fa(password.value)
        if "error" in result:
            err_lbl.text = result["error"]
        else:
            ui.notify("2FA verified — authenticated!", type="positive")
            on_done()

    ui.button("Verify Password", on_click=verify).classes(
        "bg-blue-700 text-white mt-3 px-4 py-2 w-full"
    )


def _render_connected(reader):
    # Consistent button/badge height — use ui.button style for all three
    _BTN = "text-xs px-3 py-1 min-h-0"

    with ui.row().classes("w-full items-center gap-2 mb-3"):
        # Connected indicator — same height/padding as the adjacent buttons
        ui.button("● Connected").classes(
            f"bg-green-700 text-white {_BTN} cursor-default"
        ).props("disable flat").style("opacity:1; pointer-events:none")

        async def do_disconnect():
            await reader.disconnect()
            ui.notify("Disconnected", type="info")
        ui.button("Disconnect", on_click=do_disconnect).classes(
            f"bg-gray-700 text-white {_BTN}"
        )

        async def do_reset():
            await reader.reset_session()
            ui.notify("Session reset", type="warning")
        ui.button("Reset Session", on_click=do_reset).classes(
            f"bg-red-800 text-white {_BTN}"
        )

        # do_dc_check references _dc_lbl by closure; _dc_lbl is defined after
        # the button so it renders immediately to its right in the flex row.
        async def do_dc_check():
            _dc_lbl.text = "Checking..."
            info = await reader.get_dc_info()
            if "error" in info:
                _dc_lbl.text = f"DC: {info['error']}"
                return
            parts = [f"Session: DC{info['session_dc']}"]
            for ch in info["channels"]:
                ch_dc = ch["channel_dc"]
                label = f"{ch['group_name'][:20]}: DC{ch_dc}" if ch_dc else f"{ch['group_name'][:20]}: unknown DC"
                if ch["mismatch"]:
                    label += " !"
                parts.append(label)
            _dc_lbl.text = " | ".join(parts)
            if any(c["mismatch"] for c in info["channels"]):
                ui.notify(
                    "DC mismatch detected — session and channel are on different DCs. "
                    "Check the app log for migration advice.",
                    type="warning", timeout=8000,
                )
            else:
                ui.notify(f"DC optimal — all on DC{info['session_dc']}", type="positive")

        ui.button("DC Check", on_click=do_dc_check).classes(
            f"bg-teal-800 text-white {_BTN}"
        ).tooltip("Check Telegram datacenter alignment for minimum latency")

        _dc_lbl = ui.label("").classes("text-xs text-yellow-300 ml-2")

    # ── Live slot status (auto-refreshing) ────────────────────────────────────
    slot_status_row = ui.row().classes("w-full items-center gap-4 mb-2 px-1 flex-wrap")

    async def _update_slot_status():
        # Offloaded — reader.get_status() runs a SELECT COUNT(*) against the
        # telegram_messages table; doing that synchronously every 2s directly
        # on the event loop blocked the whole app for its duration.
        status = await tg_controller.get_reader_status(reader)
        slot_status_row.clear()
        with slot_status_row:
            for s_info in status.get("slots", []):
                active = s_info.get("listener_active", False)
                name   = (s_info.get("group_name") or "not selected")[:35]
                dot_col = "bg-green-400" if active else "bg-gray-600"
                txt_col = "text-green-300" if active else "text-gray-500"
                state_lbl = "LIVE" if active else "idle"
                with ui.row().classes("items-center gap-1"):
                    ui.element("div").classes(
                        f"w-2 h-2 rounded-full shrink-0 {dot_col}"
                    )
                    ui.label(f"Slot {s_info['slot']}: {name} ({state_lbl})").classes(
                        f"text-xs {txt_col}"
                    )

    asyncio.create_task(_update_slot_status())
    ui.timer(2.0, _update_slot_status)

    # ── Pending unrecognised messages ─────────────────────────────────────────
    _pending_card = ui.card().classes(
        "w-full bg-yellow-900 border border-yellow-600 px-4 py-3 rounded-lg mb-2"
    ).style("display:none")
    _pending_container = ui.column().classes("w-full gap-2")

    async def _refresh_pending():
        rows = await tg_controller.get_pending_unrecognised(limit=20)
        if not rows:
            _pending_card.style("display:none")
            return
        _pending_card.style("")
        _pending_container.clear()
        with _pending_container:
            with ui.row().classes("items-center gap-2 mb-1"):
                ui.icon("warning").classes("text-yellow-400")
                ui.label(f"{len(rows)} unrecognised message(s) need review").classes(
                    "text-sm font-semibold text-yellow-300"
                )
            for row in rows:
                _render_pending_question(row, _refresh_pending)

    with _pending_card:
        _pending_container

    ui.timer(5.0, _refresh_pending)
    asyncio.create_task(_refresh_pending())

    # ── Group selector (compact) ───────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 px-4 py-2 rounded-lg mb-2"):
        with ui.row().classes("w-full items-center gap-3 flex-wrap"):
            ui.label("Change channel:").classes("text-xs text-gray-400 shrink-0")
            ui.space()
            slot_sel       = ui.select(
                {1: "Slot 1", 2: "Slot 2", 3: "Slot 3"}, value=1, label="Slot"
            ).classes("w-24")
            groups_dd_wrap = ui.column().classes("flex-1 min-w-40")
            load_btn       = ui.button("Load Groups", icon="refresh").classes(
                "bg-blue-700 text-white text-xs px-3 py-1"
            )

        groups_container = ui.row().classes("w-full gap-2 items-center flex-wrap pb-1")

        async def load_groups():
            groups_container.clear()
            with groups_container:
                ui.spinner(size="sm")
            groups = await reader.get_groups()
            groups_container.clear()
            if not groups:
                with groups_container:
                    ui.label("No groups found.").classes("text-gray-500 text-xs")
                return
            opts     = {str(g["id"]): g["name"] for g in groups}
            with groups_container:
                group_dd = ui.select(opts, label="Select group").classes("flex-1 min-w-48")

                async def on_select():
                    if not group_dd.value:
                        return
                    gid   = int(group_dd.value)
                    gname = opts[group_dd.value]
                    await reader.select_group(gid, gname, slot_sel.value)
                    reader.save_group_selections()
                    ui.notify(f"Slot {slot_sel.value}: {gname}", type="positive")
                    await reader.start_listener(slot_sel.value)

                ui.button("Apply", on_click=on_select).classes(
                    "bg-green-700 text-white text-xs px-3 py-1"
                )

        load_btn.on("click", lambda: asyncio.create_task(load_groups()))

    # ── Split message feeds ────────────────────────────────────────────────────
    with ui.row().classes("w-full gap-3 items-start"):
        _render_slot_feed(reader, slot=1)
        _render_slot_feed(reader, slot=2)
        _render_slot_feed(reader, slot=3)

    # ── Stored Messages (SQLite) ───────────────────────────────────────────────
    _render_stored_messages()
