"""Parsing settings and the logic-keyword editor."""
from nicegui import ui

from backend.src.controllers import telegram_controller as tg_controller
from backend.src.controllers import telegram_controller as logic_kw
# (toggle_key, label, description, default) grouped by category -- persisted
# immediately on change, matching the existing Toxic-Hour Blocklist toggle's
# convention (Risk Settings tab), not the Logic Keywords lexicon boxes below
# (which need the SAVE button). Auto-Execution / Immediate Market Buy-Sell /
# Entry Realignment used to be hand-rolled cards in their own separate grid;
# they are ordinary rows here now so every switch on the page renders through
# one code path and lines up on one grid.
_PARSING_CATEGORIES: list[tuple[str, str, list[tuple[str, str, str, int]]]] = [
    ("PARSING", "text-violet-300 bg-violet-500/15", [
        ("lk_enable_tp_parsing", "Enable TP Parsing",
         "Use this signal's own stated TP levels when a new entry parses. "
         "When OFF, TP levels are stripped before execution.", 1),
        ("lk_enable_sl_parsing", "Enable SL Parsing",
         "Use this signal's own stated Stop Loss when a new entry parses. "
         "When OFF, it is replaced by the channel template's SL Pips, or the "
         "Fallback SL Distance below. Applies to new entries only — the "
         "RISK FREE / BE and CLOSE ALL triggers are separate.", 1),
        ("lk_enable_second_message_tp_sl", "TP/SL in Second Message",
         "Hold a signal that arrives with a direction and entry but no levels, "
         "and complete it from a follow-up message sent within the match window "
         "below. Executes bare if nothing arrives in time.", 0),
    ]),
    ("EXECUTION", "text-cyan-300 bg-cyan-500/15", [
        ("auto_execute_signals", "Auto-Execution",
         "Incoming Telegram signals are automatically traded when ON. Manual "
         "signals always require explicit execution. Ensure Algo Trading is "
         "enabled in the MT5 Terminal — MT5 disables it automatically after "
         "restarts or account switches.", 0),
        ("lk_enable_close_all_parsing", "Enable CLOSE ALL Parsing",
         "Automatically close the triggering channel's own open trade when it "
         "sends a CLOSE ALL trigger phrase.", 1),
        ("lk_enable_risk_free_be_parsing", "Enable RISK FREE / BE Parsing",
         "Move SL to entry price (breakeven) when the channel sends a "
         "breakeven/risk-free trigger phrase.", 1),
        ("immediate_market_entry", "Immediate Market Buy/Sell",
         "Reads all Telegram channels for bare 'Buy Now'/'Sell Now' messages and "
         "enters at current market price immediately. When the follow-up signal "
         "with SL and TP levels arrives the open trade is updated automatically. "
         "Applies to all strategies.", 0),
        ("lk_entry_realignment", "Entry Realignment",
         "Limit Runner only. If the market has already moved through the "
         "signalled zone by the time the order would be placed, enters at "
         "current market price instead and shifts SL/TP by the same distance — "
         "otherwise the broker rejects a now-invalid limit price and the trade "
         "is lost entirely.", 0),
        ("lk_enable_mirror_copy", "Reverse / Mirror Copy",
         "Invert BUY↔SELL and mirror Stop Loss and every TP through the entry "
         "zone, so the trade placed is the exact opposite of the one signalled. "
         "Applies to every channel.", 0),
    ]),
    ("SAFETY", "text-emerald-300 bg-emerald-500/15", [
        ("lk_ignore_media_messages", "Ignore Media Messages",
         "Ignore messages containing photos, videos, or documents (only parse "
         "plain text).", 1),
        ("lk_ignore_forwarded_messages", "Ignore Forwarded Messages",
         "Do not execute trades from messages forwarded from other channels.", 0),
    ]),
    ("MARKET GUARD", "text-amber-300 bg-amber-500/15", [
        ("lk_queue_closed_market_limits", "Queue Closed Market Limits",
         "Hold BUY/SELL LIMIT signals that arrive while the market is shut for "
         "the weekend, then place them automatically when it reopens. Without "
         "this they are dropped and the setup is lost.", 0),
    ]),
]
_CARD = "bg-gray-900 p-3 rounded-lg h-full flex flex-col"


def _render_toggle_card(key: str, label: str, desc: str, default: int,
                        badge: str, badge_cls: str, rs: dict) -> ui.switch:
    """One switch card. h-full on the card plus items-stretch on the grid is
    what keeps every card in a row the same height regardless of how long its
    description runs — without it the grid rows ragged out badly once the
    descriptions stopped being roughly equal length."""
    with ui.card().classes(_CARD):
        with ui.row().classes("w-full items-start justify-between gap-2 no-wrap"):
            sw = ui.switch(label, value=bool(rs.get(key, default))).classes("text-sm")
            ui.label(badge).classes(
                f"text-[10px] font-bold tracking-wider px-1.5 py-0.5 rounded shrink-0 {badge_cls}"
            )
        ui.label(desc).classes("text-xs text-gray-500 mt-1")

        def _on_toggle(e, key=key, label=label):
            tg_controller.update_risk_settings({key: 1 if e.value else 0})
            ui.notify(f"{label} {'enabled' if e.value else 'disabled'}",
                      type="positive" if e.value else "info")
        sw.on_value_change(_on_toggle)
    return sw


def _render_parsing_settings_section() -> None:
    rs = tg_controller.get_risk_settings()

    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("items-center gap-2 mb-3"):
            ui.icon("tune", size="sm").classes("text-yellow-400")
            ui.label("Parsing Settings").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "How incoming Telegram messages are read and acted on — "
                "independent of any per-channel learned rules."
            )

        # ── Toggles, grouped by category ───────────────────────────────────
        for badge, badge_cls, toggles in _PARSING_CATEGORIES:
            ui.label(badge).classes(
                "text-xs font-bold tracking-wider text-gray-400 mt-2 mb-1"
            )
            with ui.grid(columns=3).classes("w-full gap-3 items-stretch"):
                for key, label, desc, default in toggles:
                    _render_toggle_card(key, label, desc, default, badge, badge_cls, rs)

        # ── Match window (TP/SL in Second Message) ─────────────────────────
        with ui.row().classes("items-center gap-2 mt-3"):
            ui.label("Second-message match window:").classes("text-xs text-gray-400")
            window_in = ui.number(
                value=int(rs.get("lk_second_message_match_window_sec", 120)),
                min=1, max=3600, step=10, format="%d",
            ).classes("w-24").props("outlined dense")
            ui.label("sec").classes("text-xs text-gray-400")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "How long a signal with no SL/TP is held while its follow-up "
                "message is awaited. Applies only when TP/SL in Second Message "
                "is enabled."
            )

            def _on_window(e):
                try:
                    val = max(1, min(3600, int(e.value)))
                except (TypeError, ValueError):
                    return
                tg_controller.update_risk_settings({"lk_second_message_match_window_sec": val})
                ui.notify(f"Match window set to {val}s", type="positive")
            window_in.on_value_change(_on_window)

        # ── Fallback SL distance (Enable SL Parsing OFF) ───────────────────
        with ui.row().classes("items-center gap-2 mt-3"):
            ui.label("Fallback SL distance:").classes("text-xs text-gray-400")
            fb_sl_in = ui.number(
                value=float(rs.get("lk_fallback_sl_pips", 50.0)),
                min=1, max=1000, step=5, format="%.0f",
            ).classes("w-24").props("outlined dense")
            ui.label("pips").classes("text-xs text-gray-400")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Stop distance used in place of the signal's own when Enable "
                "SL Parsing is OFF, measured from the far edge of the entry "
                "zone. A channel with an EA Template uses that template's own "
                "SL Pips instead. Ignored entirely while SL Parsing is ON."
            )

            def _on_fb_sl(e):
                try:
                    val = max(1.0, min(1000.0, float(e.value)))
                except (TypeError, ValueError):
                    return
                tg_controller.update_risk_settings({"lk_fallback_sl_pips": val})
                ui.notify(f"Fallback SL distance set to {val:.0f} pips", type="positive")
            fb_sl_in.on_value_change(_on_fb_sl)

        # ── Logic Keywords ─────────────────────────────────────────────────
        with ui.row().classes("items-center gap-2 mt-5 mb-2"):
            ui.icon("key", size="sm").classes("text-yellow-400")
            ui.label("Logic Keywords").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Global, editable trigger phrases used by the Telegram message "
                "parser — independent of any per-channel learned rules."
            )

        # ── Lexicon boxes ──────────────────────────────────────────────────
        lexicons = logic_kw.get_all_lexicons()
        boxes: dict[str, object] = {}
        with ui.grid(columns=2).classes("w-full gap-4 items-stretch"):
            # Driven off DEFAULT_LEXICONS rather than a hand-written tuple:
            # a lexicon missing from this list is one the user cannot edit,
            # and since 2026-08-27 buy_orders/sell_orders are live market-
            # entry triggers -- an invisible one is a trigger nobody can
            # turn off. Pinned by tests/frontend/test_parsing_settings_render.
            for category in logic_kw.DEFAULT_LEXICONS:
                with ui.column().classes("gap-1 h-full"):
                    ui.label(logic_kw.LEXICON_LABELS[category]).classes(
                        "text-sm font-semibold text-cyan-300"
                    )
                    ui.label(logic_kw.LEXICON_HELP[category]).classes(
                        "text-xs text-gray-500 flex-1"
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
