"""Telegram page — auth wizard, group selector, live message feed."""

import asyncio
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from nicegui import ui

from forex_trader.core import database as db_module
from forex_trader.core import core_logic_keywords as logic_kw
from forex_trader.core.telegram_reader import (
    AUTH_DISCONNECTED, AUTH_AWAITING_CODE, AUTH_AWAITING_2FA,
    AUTH_CONNECTED, AUTH_RECONNECTING, AUTH_FAILED,
)
from frontend.pages.trading import render_signals_card

def _ts(s) -> str:
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return str(s)[:8]


# (toggle_key, label, tooltip) -- persisted immediately on change, matching
# the existing Toxic-Hour Blocklist toggle's convention (Risk Settings tab),
# not the Logic Keywords lexicon boxes below (which need the SAVE button).
_LK_TOGGLES: list[tuple[str, str, str]] = [
    ("lk_enable_tp_parsing", "Enable TP Parsing",
     "Use this signal's own stated TP levels when a new entry parses. "
     "When OFF, TP levels are stripped before execution."),
    ("lk_enable_close_all_parsing", "Enable CLOSE ALL Parsing",
     "Automatically close the triggering channel's own open trade when it "
     "sends a CLOSE ALL trigger phrase."),
    ("lk_ignore_forwarded_messages", "Ignore Forwarded Messages",
     "Do not execute trades from messages forwarded from other channels."),
    ("lk_enable_tp_hit_parsing", "Enable TP HIT Parsing",
     "Detect and log/notify \"TP1 HIT\"-style messages. Never moves SL or "
     "closes anything by itself."),
    ("lk_enable_risk_free_be_parsing", "Enable RISK FREE / BE Parsing",
     "Move SL to entry price (breakeven) when the channel sends a "
     "breakeven/risk-free trigger phrase."),
    ("lk_enable_sl_parsing", "Enable SL Parsing",
     "Use this signal's own stated Stop Loss when a new entry parses. "
     "When OFF, Stop Loss is stripped before execution."),
    ("lk_ignore_media_messages", "Ignore Media Messages",
     "Ignore messages containing photos, videos, or documents (only parse plain text)."),
]


def _render_logic_keywords_section() -> None:
    rs = db_module.get_risk_settings()

    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("key", size="sm").classes("text-yellow-400")
            ui.label("Logic Keywords").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Global, editable trigger phrases used by the Telegram message "
                "parser — independent of any per-channel learned rules."
            )

        # ── Toggles ────────────────────────────────────────────────────────
        with ui.grid(columns=3).classes("w-full gap-3 mb-4"):
            for key, label, tip in _LK_TOGGLES:
                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    sw = ui.switch(label, value=bool(rs.get(key, 1))).classes("text-sm")
                    ui.label(tip).classes("text-xs text-gray-500 mt-1")

                    def _on_toggle(e, key=key, label=label):
                        db_module.update_risk_settings({key: 1 if e.value else 0})
                        ui.notify(f"{label} {'enabled' if e.value else 'disabled'}",
                                 type="positive" if e.value else "info")
                    sw.on_value_change(_on_toggle)

        # ── Auto-Execution / Immediate Market Buy-Sell / Entry Realignment
        # (moved here from Trading > Strategy, 2026-07-23; restyled to match
        # the Logic Keywords toggle cards above, 2026-07-23; no separator
        # from the toggles above -- sits within the same section, 2026-07-23) ──
        with ui.grid(columns=3).classes("w-full gap-3 mb-4"):
            with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                auto_sw = ui.switch(
                    "Auto-Execution", value=bool(rs.get("auto_execute_signals", 0)),
                ).classes("text-sm")
                ui.label(
                    "Incoming Telegram signals are automatically traded when ON. "
                    "Manual signals always require explicit execution. Ensure "
                    "Algo Trading is enabled in the MT5 Terminal — MT5 disables "
                    "it automatically after restarts or account switches."
                ).classes("text-xs text-gray-500 mt-1")

                def _on_auto_toggle(e):
                    db_module.update_risk_settings({"auto_execute_signals": 1 if e.value else 0})
                    ui.notify(f"Auto-execution {'enabled' if e.value else 'disabled'}",
                             type="positive" if e.value else "info")
                auto_sw.on_value_change(_on_auto_toggle)

            with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                ime_sw = ui.switch(
                    "Immediate Market Buy/Sell", value=bool(rs.get("immediate_market_entry", 0)),
                ).classes("text-sm")
                ui.label(
                    "Reads all Telegram channels for bare 'Buy Now'/'Sell Now' "
                    "messages and enters at current market price immediately. "
                    "When the follow-up signal with SL and TP levels arrives the "
                    "open trade is updated automatically. Applies to all strategies."
                ).classes("text-xs text-gray-500 mt-1")

                def _on_ime_toggle(e):
                    db_module.update_risk_settings({"immediate_market_entry": 1 if e.value else 0})
                    ui.notify(f"Immediate market entry {'enabled' if e.value else 'disabled'}",
                             type="positive" if e.value else "info")
                ime_sw.on_value_change(_on_ime_toggle)

            with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                realign_sw = ui.switch(
                    "Entry Realignment", value=bool(rs.get("lk_entry_realignment", 0)),
                ).classes("text-sm")
                ui.label(
                    "Limit Runner only. If the market has already moved through "
                    "the signalled zone by the time the order would be placed, "
                    "enters at current market price instead and shifts SL/TP by "
                    "the same distance — otherwise the broker rejects a "
                    "now-invalid limit price and the trade is lost entirely."
                ).classes("text-xs text-gray-500 mt-1")

                def _on_realign_toggle(e):
                    db_module.update_risk_settings({"lk_entry_realignment": 1 if e.value else 0})
                    ui.notify(f"Entry realignment {'enabled' if e.value else 'disabled'}",
                             type="positive" if e.value else "info")
                realign_sw.on_value_change(_on_realign_toggle)

        # ── Lexicon boxes ──────────────────────────────────────────────────
        lexicons = logic_kw.get_all_lexicons()
        boxes: dict[str, object] = {}
        with ui.grid(columns=2).classes("w-full gap-4"):
            for category in ("symbol_tokens", "close_all", "risk_free_be",
                             "exclusion", "buy_orders", "limit_orders"):
                with ui.column().classes("gap-1"):
                    ui.label(logic_kw.LEXICON_LABELS[category]).classes(
                        "text-sm font-semibold text-cyan-300"
                    )
                    ui.label(logic_kw.LEXICON_HELP[category]).classes(
                        "text-xs text-gray-500"
                    )
                    boxes[category] = ui.textarea(
                        value=", ".join(lexicons[category]),
                    ).classes("w-full font-mono text-sm").props("outlined dense rows=3")

        def _save():
            try:
                for category, box in boxes.items():
                    phrases = [p.strip() for p in str(box.value or "").split(",")]
                    logic_kw.set_lexicon(category, phrases)
                ui.notify("Logic Keywords saved", type="positive")
            except Exception as exc:
                ui.notify(f"Save failed: {exc}", type="negative")

        ui.button("Save", icon="save", on_click=_save).classes(
            "bg-blue-700 text-white mt-3 px-4 py-2"
        )


def _render_channels_active_section(reader) -> None:
    """One toggle per loaded Telegram channel — turns parsing/execution off
    for that channel entirely (the listener keeps running, messages are
    just ignored) without touching the other channels or disconnecting.
    Wired to channel_parser_config.enabled, the same column engine.py's
    _scan_messages already gates every message on (see the `not bool(ch_cfg.
    get('enabled', 1))` skip) — this only adds a visible, dedicated place to
    flip it, no new backend mechanism. Names are read live from the
    Telegram reader's own resolved group titles every render, never
    hardcoded, so a channel rename shows up here automatically."""
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("hub", size="sm").classes("text-yellow-400")
            ui.label("Channels Active").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Turn a channel off to stop parsing and executing signals from it "
                "entirely — the Telegram listener keeps running, messages from "
                "that channel are just ignored. Names update automatically if a "
                "channel is renamed on Telegram's side."
            )

        status = reader.get_status() if reader else {"slots": []}
        slots = [s for s in status.get("slots", []) if s.get("group_name")]

        if not slots:
            ui.label(
                "No channels loaded yet — select a group in a slot below."
            ).classes("text-xs text-gray-500 italic")
            return

        with ui.grid(columns=3).classes("w-full gap-3"):
            for s in slots:
                channel_name = s["group_name"]
                cfg = db_module.get_channel_parser_config(channel_name) or {}
                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    sw = ui.switch(
                        channel_name, value=bool(cfg.get("enabled", 1)),
                    ).classes("text-sm")
                    ui.label(f"Slot {s['slot']}").classes("text-xs text-gray-500 mt-1")

                    def _on_toggle(e, ch=channel_name, existing=cfg):
                        db_module.save_channel_parser_config(
                            ch,
                            existing.get("parser_format", "auto"),
                            existing.get("signal_prefix", ""),
                            bool(existing.get("instant_entry_enabled", 0)),
                            bool(e.value),
                            existing.get("notes", ""),
                        )
                        ui.notify(
                            f"{ch} {'enabled' if e.value else 'disabled'} — "
                            + ("signals will execute normally" if e.value
                               else "signals from this channel will be ignored"),
                            type="positive" if e.value else "warning",
                        )
                    sw.on_value_change(_on_toggle)


def render(get_tg_reader: Callable):
    reader = get_tg_reader()

    render_signals_card()
    _render_logic_keywords_section()
    _render_channels_active_section(reader)

    # ── Status banner ──────────────────────────────────────────────────────────
    status_badge = ui.badge("Disconnected", color="red").classes("text-sm mb-3")
    error_lbl    = ui.label("").classes("text-red-300 text-sm")

    def _update_status_badge():
        state = reader.auth_state
        err   = reader.auth_error or ""
        colours = {
            AUTH_DISCONNECTED:  ("red",    "Disconnected"),
            AUTH_AWAITING_CODE: ("orange", "Awaiting Code"),
            AUTH_AWAITING_2FA:  ("orange", "Awaiting 2FA"),
            AUTH_CONNECTED:     ("green",  "Connected"),
            AUTH_RECONNECTING:  ("blue",   "Reconnecting"),
            AUTH_FAILED:        ("red",    "Failed"),
        }
        colour, label = colours.get(state, ("grey", state))
        status_badge.props(f"color={colour}")
        status_badge.text  = label
        # Hide top badge when connected — the inline badge next to Disconnect shows instead
        status_badge.style("display:none" if state == AUTH_CONNECTED else "")
        error_lbl.text     = err

    # ── Auth wizard (re-renders on each state change) ─────────────────────────
    main_container   = ui.column().classes("w-full")
    _rendered_state  = [None]

    def _render_wizard():
        state = reader.auth_state
        _update_status_badge()
        if state == _rendered_state[0]:
            return
        _rendered_state[0] = state
        main_container.clear()
        with main_container:
            if state == AUTH_CONNECTED:
                ui.notify("Telegram connected!", type="positive")
                _render_connected(reader)
            else:
                with ui.card().classes("w-full max-w-lg bg-gray-800 p-6 rounded-lg mt-4"):
                    ui.label("Telegram Authentication").classes(
                        "text-lg font-bold text-yellow-300 mb-4"
                    )
                    if state in (AUTH_DISCONNECTED, AUTH_FAILED):
                        _render_send_code_step(reader, _render_wizard)
                    elif state == AUTH_AWAITING_CODE:
                        _render_verify_code_step(reader, _render_wizard)
                    elif state == AUTH_AWAITING_2FA:
                        _render_verify_2fa_step(reader, _render_wizard)

    ui.timer(1.0, _render_wizard)
    _render_wizard()


def _render_send_code_step(reader, on_done: Callable):
    import backend.src.config as cfg_module
    cfg = cfg_module.load()

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
        status = await db_module.to_db_thread(reader.get_status)
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
        rows = await db_module.to_db_thread(db_module.get_pending_unrecognised_messages, limit=20)
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


def _render_slot_feed(reader, slot: int):
    with ui.card().classes("flex-1 bg-gray-800 p-0 rounded-lg overflow-hidden min-w-64"):
        # Feed header — rebuilt every refresh so group name stays current
        header_row = ui.row().classes(
            "w-full px-4 py-2 items-center justify-between gap-2"
        ).style("background:#1e2433")
        feed       = ui.scroll_area().classes("w-full bg-gray-900 p-2").style("height:600px")

        async def refresh():
            # Read current slot info fresh every tick — offloaded, see
            # _update_slot_status for why reader.get_status() must not run
            # directly on the event loop.
            status = await db_module.to_db_thread(reader.get_status)
            slot_info_now = next(
                (s for s in status.get("slots", []) if s.get("slot") == slot), {}
            )
            gname     = slot_info_now.get("group_name") or f"Slot {slot} — not selected"
            gid       = str(slot_info_now.get("group_id") or "")
            is_active = bool(slot_info_now.get("listener_active", False))

            # Update header
            header_row.clear()
            with header_row:
                with ui.row().classes("items-center gap-2"):
                    ui.element("div").classes(
                        "w-2 h-2 rounded-full " + ("bg-green-400" if is_active else "bg-gray-600")
                    )
                    ui.label(gname[:40]).classes(
                        "text-sm font-semibold text-gray-200 truncate"
                    )
                count_lbl = ui.label("").classes("text-xs text-gray-500")

            # Fetch the full buffer then filter per-slot, giving each slot up to 100 msgs.
            all_msgs = reader.get_buffer_messages(250)
            msgs = [m for m in all_msgs if str(m.get("group_id", "")) == gid] if gid else all_msgs
            msgs = msgs[:100]
            count_lbl.text = str(len(msgs))

            feed.clear()
            with feed:
                if not msgs:
                    ui.label("No messages yet.").classes(
                        "text-gray-500 text-sm italic p-2"
                    )
                    return
                for m in msgs:
                    ts     = _ts(m.get("timestamp") or m.get("received_at"))
                    sender = (m.get("sender_name") or "?")[:22]
                    text   = (m.get("text") or "")[:300]
                    with ui.column().classes("w-full border-b border-gray-700 py-1 gap-0"):
                        with ui.row().classes("gap-2 text-xs"):
                            ui.label(ts).classes("text-gray-500 shrink-0")
                            ui.label(f"{sender}:").classes("text-yellow-300 font-semibold")
                        ui.label(text).classes("text-gray-300 text-xs break-words")

        ui.timer(2.0, refresh)
        asyncio.create_task(refresh())


def _ts_uk(s) -> str:
    """Format a stored ISO timestamp to local time for the SQLite table."""
    if not s:
        return "—"
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone().strftime("%m-%d %H:%M:%S")
    except Exception:
        return str(s)[:16]


def _render_stored_messages():
    """Collapsible section showing the last 100 messages stored in telegram_messages."""
    import backend.src.config as _cfg

    def _query_messages(limit: int = 100) -> tuple[list[dict], int]:
        try:
            from backend.src.config import DATA_DIR
            env = _cfg.get("account_env", "demo")
            db_path = str(DATA_DIR / f"forex_trader_{env}.db")
            conn = sqlite3.connect(db_path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            total = conn.execute("SELECT COUNT(*) FROM telegram_messages").fetchone()[0]
            rows  = conn.execute(
                "SELECT group_name, sender_name, timestamp, received_at, text, "
                "       has_media, media_type "
                "FROM telegram_messages "
                "ORDER BY id DESC LIMIT ?",
                (limit,),
            ).fetchall()
            conn.close()
            return [dict(r) for r in rows], total
        except Exception:
            return [], 0

    with ui.expansion("Stored Messages (SQLite)", icon="storage").classes(
        "w-full bg-gray-800 rounded-lg mt-3"
    ):
        container = ui.column().classes("w-full")
        footer_lbl = ui.label("").classes("text-xs text-gray-500 italic px-2 py-1")

        def _build():
            container.clear()
            rows, total = _query_messages(100)
            footer_lbl.text = f"Showing {min(len(rows), 100)} of {total} stored messages"

            if not rows:
                with container:
                    ui.label("No stored messages.").classes("text-gray-500 italic text-sm p-2")
                return

            with container:
                # Header row
                with ui.row().classes(
                    "w-full gap-0 px-2 py-1 text-xs text-gray-500 font-semibold "
                    "uppercase tracking-wider border-b border-gray-700"
                ):
                    ui.label("Local Time").classes("w-36 shrink-0")
                    ui.label("Group").classes("w-40 shrink-0 truncate")
                    ui.label("Sender").classes("w-40 shrink-0 truncate")
                    ui.label("Text").classes("flex-1 min-w-0")
                    ui.label("Media").classes("w-16 shrink-0 text-right")

                # Data rows (most recent first)
                for r in rows:
                    ts_val   = r.get("timestamp") or r.get("received_at") or ""
                    group    = (r.get("group_name") or "?")[:30]
                    sender   = (r.get("sender_name") or "?")[:30]
                    text     = (r.get("text") or "").replace("\n", " ")[:200]
                    media    = r.get("media_type") or ("photo" if r.get("has_media") else "none")
                    with ui.row().classes(
                        "w-full gap-0 px-2 py-1 text-xs border-b border-gray-700 "
                        "hover:bg-gray-750"
                    ).style("background:transparent"):
                        ui.label(_ts_uk(ts_val)).classes("w-36 shrink-0 font-mono text-gray-400")
                        ui.label(group).classes("w-40 shrink-0 text-gray-300 truncate")
                        ui.label(sender).classes("w-40 shrink-0 text-gray-400 truncate")
                        ui.label(text).classes("flex-1 min-w-0 text-gray-200 break-words")
                        ui.label(media).classes("w-16 shrink-0 text-right text-gray-500")

        _build()
        footer_lbl


def _render_pending_question(row: dict, refresh: Callable) -> None:
    unrec_id     = row["id"]
    channel_name = row.get("channel_name", "?")
    raw_text     = row.get("raw_text", "")
    analysis_raw = row.get("claude_analysis") or ""
    analysis: dict = {}
    if analysis_raw:
        try:
            analysis = json.loads(analysis_raw)
        except Exception:
            pass

    summary    = analysis.get("summary", "Analysing...") if analysis else "Analysing..."
    suggested  = analysis.get("suggested_action", "review_manually")
    confidence = analysis.get("confidence", 0.0)
    reasoning  = analysis.get("reasoning", "")

    with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
        with ui.row().classes("items-center gap-2 mb-1 flex-wrap"):
            ui.label(channel_name[:30]).classes(
                "text-xs font-semibold text-yellow-300 shrink-0"
            )
            if confidence:
                ui.badge(f"{int(confidence * 100)}% confidence", color="grey").classes(
                    "text-xs shrink-0"
                )

        ui.label(summary).classes("text-xs text-gray-300 mb-1")
        if reasoning:
            ui.label(reasoning[:120]).classes("text-xs text-gray-500 italic mb-1")

        # Raw message preview (collapsed)
        with ui.expansion("View message", icon="chat").classes("w-full text-xs"):
            ui.label(raw_text[:500]).classes(
                "text-xs font-mono text-gray-400 whitespace-pre-wrap break-words p-2"
            )

        with ui.row().classes("gap-2 mt-2 flex-wrap"):
            def _resolve(resolution: str, rid=unrec_id, ch=channel_name, rt=raw_text):
                # Store learned rule from this resolution
                rule_notes = f"User resolved: {resolution}"
                pattern    = rt.split("\n")[0][:80]
                db_module.save_channel_learned_rule(
                    ch, "ignore_pattern" if resolution == "ignore" else "parser_format",
                    pattern, resolution, rule_notes, str(rid),
                )
                db_module.update_unrecognised_message(
                    rid, resolution=resolution, status="resolved"
                )
                # Update channel config if a format was chosen
                if resolution in ("format_ab", "gd2"):
                    existing = db_module.get_channel_parser_config(ch)
                    if existing:
                        db_module.save_channel_parser_config(
                            ch, resolution,
                            existing.get("signal_prefix", ""),
                            bool(existing.get("instant_entry_enabled", 0)),
                            True,
                            f"Format updated from unrecognised message review",
                        )
                ui.notify(f"Resolved as: {resolution}", type="positive")
                asyncio.create_task(refresh())

            ui.button(
                "Ignore this type", icon="block",
                on_click=lambda: _resolve("ignore"),
            ).classes("bg-gray-700 text-white text-xs px-2 py-1")

            ui.button(
                "Format A/B", icon="check",
                on_click=lambda: _resolve("format_ab"),
            ).classes("bg-blue-800 text-white text-xs px-2 py-1")

            ui.button(
                "GD2 format", icon="check",
                on_click=lambda: _resolve("gd2"),
            ).classes("bg-purple-800 text-white text-xs px-2 py-1")

            ui.button(
                "Dismiss", icon="close",
                on_click=lambda: (
                    db_module.update_unrecognised_message(unrec_id, status="dismissed"),
                    asyncio.create_task(refresh()),
                ),
            ).classes("bg-gray-600 text-white text-xs px-2 py-1")
