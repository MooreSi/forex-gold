"""The live channel feed: active channels, slot feed, stored messages and
the pending-question prompt."""
import asyncio
from typing import Callable
import json
from datetime import datetime, timezone

from nicegui import ui

from backend.src.controllers import telegram_controller as tg_controller

import logging

_log = logging.getLogger(__name__)


def _ts(s) -> str:
    if not s:
        return ""
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00")).astimezone()
        return dt.strftime("%H:%M:%S")
    except Exception:
        return str(s)[:8]


def _save_channel_flags(channel_name: str, existing: dict, *,
                        instant_entry: bool, enabled: bool) -> None:
    """Write one channel's two switches back, carrying the rest of its row.

    save_channel_parser_config takes six POSITIONAL arguments and the two this
    card owns are adjacent booleans. Keyword-only here so a caller cannot pass
    them the wrong way round: swapping them would disable a channel while
    reporting that instant entry changed, and both calls would still run.
    """
    tg_controller.save_channel_parser_config(
        channel_name,
        existing.get("parser_format", "auto"),
        existing.get("signal_prefix", ""),
        instant_entry,
        enabled,
        existing.get("notes", ""),
    )


def _render_channels_active_section(reader) -> None:
    """Two toggles per loaded Telegram channel.

    The first turns parsing/execution off for that channel entirely (the
    listener keeps running, messages are just ignored) without touching the
    other channels or disconnecting. Wired to channel_parser_config.enabled,
    the same column engine.py's _scan_messages already gates every message on
    (see the `not bool(ch_cfg.get('enabled', 1))` skip).

    The second is that channel's own Instant Market Entry flag
    (channel_parser_config.instant_entry_enabled), added 2026-09-05 for
    docs/todo/bugs/024. It is the per-channel half of the gate in
    scan_messages.py; the Parsing tab's "Immediate Market Buy/Sell" is the
    global half and both must be on. It had no control anywhere in the app,
    so the only value it ever held was whatever a channel's auto-bootstrap
    wrote the first time that channel was seen — and a channel renamed on
    Telegram's side gets a fresh row, because this config is keyed by
    channel_name and not by group_id. That is how a channel came to match a
    BUY trigger correctly and place nothing, with nothing on screen saying
    why.

    Neither switch is a new backend mechanism: both write existing columns
    through save_channel_parser_config, which already accepted both.

    Names are read live from the Telegram reader's own resolved group titles
    every render, never hardcoded, so a channel rename shows up here
    automatically."""
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("hub", size="sm").classes("text-yellow-400")
            ui.label("Channels Active").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Turn a channel off to stop parsing and executing signals from it "
                "entirely — the Telegram listener keeps running, messages from "
                "that channel are just ignored. Instant Entry is separate: it "
                "decides whether a bare direction with no levels from that "
                "channel opens a market order immediately. Names update "
                "automatically if a channel is renamed on Telegram's side."
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
                cfg = tg_controller.get_channel_parser_config(channel_name) or {}
                with ui.card().classes("bg-gray-900 p-3 rounded-lg"):
                    sw = ui.switch(
                        channel_name, value=bool(cfg.get("enabled", 1)),
                    ).classes("text-sm")
                    ui.label(f"Slot {s['slot']}").classes("text-xs text-gray-500 mt-1")

                    ime = ui.switch(
                        "Instant Entry",
                        value=bool(cfg.get("instant_entry_enabled", 0)),
                    ).classes("text-xs")
                    ime.tooltip(
                        "Let a bare direction from this channel — \"BUY NOW\", "
                        "\"PREPARE FOR A BUY\" — open a market order straight "
                        "away, without waiting for entry, stop and target. Off "
                        "means those messages are ignored until the full levels "
                        "arrive. The Parsing tab's Immediate Market Buy/Sell "
                        "must also be on; this is the per-channel half."
                    )

                    def _on_toggle(e, ch=channel_name, existing=cfg):
                        _save_channel_flags(
                            ch, existing,
                            instant_entry=bool(existing.get("instant_entry_enabled", 0)),
                            enabled=bool(e.value),
                        )
                        ui.notify(
                            f"{ch} {'enabled' if e.value else 'disabled'} — "
                            + ("signals will execute normally" if e.value
                               else "signals from this channel will be ignored"),
                            type="positive" if e.value else "warning",
                        )
                    sw.on_value_change(_on_toggle)

                    def _on_ime_toggle(e, ch=channel_name, existing=cfg):
                        _save_channel_flags(
                            ch, existing,
                            instant_entry=bool(e.value),
                            enabled=bool(existing.get("enabled", 1)),
                        )
                        ui.notify(
                            f"{ch} instant entry "
                            + ("ON — a bare direction from this channel will "
                               "open a market order" if e.value
                               else "off — bare directions will be ignored"),
                            type="warning" if e.value else "positive",
                        )
                    ime.on_value_change(_on_ime_toggle)


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
            status = await tg_controller.get_reader_status(reader)
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
    from backend.src.controllers import settings_controller as _cfg

    def _query_messages(limit: int = 100) -> tuple[list[dict], int]:
        return tg_controller.fetch_stored_messages(limit)

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
        except Exception as e:
            _log.debug("[telegram] decoding a stored AI analysis payload failed: %s", e)

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
                tg_controller.save_channel_learned_rule(
                    ch, "ignore_pattern" if resolution == "ignore" else "parser_format",
                    pattern, resolution, rule_notes, str(rid),
                )
                tg_controller.update_unrecognised_message(
                    rid, resolution=resolution, status="resolved"
                )
                # Update channel config if a format was chosen
                if resolution in ("format_ab", "gd2"):
                    existing = tg_controller.get_channel_parser_config(ch)
                    if existing:
                        tg_controller.save_channel_parser_config(
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
                    tg_controller.update_unrecognised_message(unrec_id, status="dismissed"),
                    asyncio.create_task(refresh()),
                ),
            ).classes("bg-gray-600 text-white text-xs px-2 py-1")
