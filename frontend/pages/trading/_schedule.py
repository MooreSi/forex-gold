"""Trading schedule, and the strategy-comparison table cells."""
from datetime import datetime, timezone
from nicegui import ui
from backend.src.controllers import trading_controller as trading_ctl

# Sibling sections of this page.


def _render_schedule():
    """Trading Schedule tab — per-day, per-window profit-target discipline
    cap on AUTOMATED order execution (manual orders are always exempt).
    Signal generation and Telegram ingestion are never affected -- see
    core_trading_schedule.py / core_signal_resolution.py for the gate this
    UI configures. Each window also independently toggles Telegram/Reversal
    Engine/Breakout Engine (2026-07-24) -- e.g. Reversal Engine performs
    well overnight but loses during London/NY, the opposite of the Telegram
    channels, so a single blanket switch isn't enough."""
    from backend.src.services.risk import schedule as sched

    schedule = sched.get_trading_schedule()
    enabled_now = sched.is_trading_schedule_enabled()
    rs = trading_ctl.get_risk_settings()

    # ── Trading Markets card ─────────────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mb-3"):
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.label("Trading Markets").classes("text-base font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Controls which trading sessions will accept and execute signals.\n\n"
                "Asia:     21:00–07:00 UTC (overnight / off-peak)\n"
                "London:   07:00–16:00 UTC (includes London/NY overlap)\n"
                "New York: 12:00–21:00 UTC (includes London/NY overlap)\n\n"
                "Signals can still be generated at any time, but they will only "
                "trigger and execute live trades during an enabled session.\n\n"
                "If more than one market is selected, the overlapping hours "
                "(12:00–16:00 UTC) are also active."
            )

        _sess_asia   = bool(rs.get("session_asia_enabled",   1))
        _sess_london = bool(rs.get("session_london_enabled", 1))
        _sess_ny     = bool(rs.get("session_ny_enabled",     1))

        def _compute_session_label() -> tuple[str, str]:
            """Return (label, badge_color) based on clock + enabled sessions."""
            from datetime import datetime, timezone as _tz
            from backend.src.services.dpm.engine import is_weekly_market_closed
            if is_weekly_market_closed():
                return "Markets Closed", "grey"
            h = datetime.now(_tz.utc).hour
            london_open = 7  <= h < 16
            ny_open     = 12 <= h < 21
            asia_open   = not (london_open or ny_open)  # 21:00-07:00 UTC

            latest = trading_ctl.get_risk_settings()
            asia_en   = bool(latest.get("session_asia_enabled",   1))
            london_en = bool(latest.get("session_london_enabled", 1))
            ny_en     = bool(latest.get("session_ny_enabled",     1))

            if london_open and ny_open:
                if london_en or ny_en:
                    return "Overlap (London + NY)", "blue"
                return "Markets Closed", "grey"
            if london_open:
                if london_en:
                    return "London", "blue"
                return "Markets Closed", "grey"
            if ny_open:
                if ny_en:
                    return "New York", "blue"
                return "Markets Closed", "grey"
            # Asian / off-hours
            if asia_en:
                return "Asia", "blue"
            return "Markets Closed", "grey"

        _init_label, _init_color = _compute_session_label()

        # Session indicator row
        with ui.row().classes("items-center gap-2 mb-1"):
            ui.label("Current session:").classes("text-xs text-gray-400")
            sess_badge = ui.badge(_init_label, color=_init_color).classes("text-xs")
        def _refresh_sess_badge(badge=sess_badge):
            lbl, col = _compute_session_label()
            badge.text = lbl
            badge.props(f"color={col}")
        ui.timer(60, _refresh_sess_badge)

        # Three market toggle buttons
        with ui.row().classes("gap-2 flex-wrap"):

            # Asia
            asia_btn = ui.button(
                "Asia",
                icon="nights_stay",
            ).props(
                f"dense {'color=green' if _sess_asia else 'flat color=grey'}"
            ).classes("text-xs font-semibold").tooltip(
                "Asian session: 21:00–07:00 UTC\n"
                "Enables signal execution during overnight / off-peak hours."
            )

            # London
            london_btn = ui.button(
                "London",
                icon="location_city",
            ).props(
                f"dense {'color=green' if _sess_london else 'flat color=grey'}"
            ).classes("text-xs font-semibold").tooltip(
                "London session: 07:00–16:00 UTC\n"
                "Includes the London/NY overlap (12:00–16:00 UTC)."
            )

            # New York
            ny_btn = ui.button(
                "New York",
                icon="location_on",
            ).props(
                f"dense {'color=green' if _sess_ny else 'flat color=grey'}"
            ).classes("text-xs font-semibold").tooltip(
                "New York session: 12:00–21:00 UTC\n"
                "Includes the London/NY overlap (12:00–16:00 UTC)."
            )

        # Status caption
        _active = []
        if _sess_asia:   _active.append("Asia")
        if _sess_london: _active.append("London")
        if _sess_ny:     _active.append("New York")
        mkt_caption = ui.label(
            f"Active: {', '.join(_active)}" if _active else "No sessions active — all signals blocked"
        ).classes("text-xs text-gray-400 mt-1" if _active else "text-xs text-red-400 mt-1")

        def _toggle_market(key: str, btn, caption=mkt_caption):
            cur = bool(trading_ctl.get_risk_settings().get(key, 1))
            new = not cur
            trading_ctl.update_risk_settings({key: 1 if new else 0})
            if new:
                btn.props("color=green")
                btn.props(remove="flat")
            else:
                btn.props("flat color=grey")
            # Rebuild caption
            latest = trading_ctl.get_risk_settings()
            parts = []
            if latest.get("session_asia_enabled",   1): parts.append("Asia")
            if latest.get("session_london_enabled", 1): parts.append("London")
            if latest.get("session_ny_enabled",     1): parts.append("New York")
            if parts:
                caption.text = f"Active: {', '.join(parts)}"
                caption.classes(replace="text-xs text-gray-400 mt-1")
            else:
                caption.text = "No sessions active — all signals blocked"
                caption.classes(replace="text-xs text-red-400 mt-1")
            # Update session badge immediately
            _refresh_sess_badge()
            sess_nm = {"session_asia_enabled": "Asia", "session_london_enabled": "London",
                       "session_ny_enabled": "New York"}[key]
            ui.notify(
                f"{sess_nm} session {'enabled' if new else 'disabled'}",
                type="positive" if new else "warning",
            )

        asia_btn.on("click",   lambda: _toggle_market("session_asia_enabled",   asia_btn))
        london_btn.on("click", lambda: _toggle_market("session_london_enabled", london_btn))
        ny_btn.on("click",     lambda: _toggle_market("session_ny_enabled",     ny_btn))

    with ui.row().classes("items-center gap-2 mb-1"):
        master_chk = ui.checkbox("Trading Schedule", value=enabled_now).classes(
            "text-cyan-300 font-bold text-lg"
        )
    ui.label(
        "Caps automated order execution per day and per time window, once a window's "
        "profit target is met trading pauses until the next window. Each window also "
        "independently allows/blocks Telegram, Reversal Engine, and Breakout Engine -- "
        "unchecking one for a window blocks only that source's live execution there, "
        "the others are unaffected. Signal generation and Telegram ingestion keep "
        "running regardless -- this only blocks the final order-placement step, and "
        "only for automated (not manual) orders."
    ).classes("text-xs text-gray-500 mb-3")

    _day_widgets: dict[str, list[dict]] = {}

    with ui.column().classes("w-full gap-2"):
        for day in sched.DAY_NAMES:
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                ui.label(day.title()).classes("font-bold text-yellow-300 text-sm mb-1")
                blocks = schedule[day]
                _day_widgets[day] = []
                for i, block in enumerate(blocks):
                    with ui.row().classes("items-center gap-2 flex-wrap"):
                        en = ui.checkbox("", value=block["enabled"]).props("dense")
                        ui.label(f"Window {i + 1}").classes("text-xs text-gray-400 w-16")
                        start = ui.input("Start", value=block["start"]).props(
                            "dense outlined"
                        ).classes("w-24").tooltip("24-hour HH:MM")
                        ui.label("to").classes("text-xs text-gray-500")
                        end = ui.input("End", value=block["end"]).props(
                            "dense outlined"
                        ).classes("w-24").tooltip("24-hour HH:MM")
                        target = ui.number(
                            "Target $", value=block["target"], min=0, step=1.0
                        ).props("dense outlined").classes("w-28").tooltip(
                            "0 = no profit cap, only the time window applies"
                        )
                        ui.label("|").classes("text-gray-600")
                        tg_chk = ui.checkbox("Telegram", value=block["telegram"]).classes(
                            "text-xs"
                        ).props("dense").tooltip(
                            "Allow automated Telegram-signal trades during this window"
                        )
                        re_chk = ui.checkbox("Reversal Engine", value=block["reversal_engine"]).classes(
                            "text-xs"
                        ).props("dense").tooltip(
                            "Allow Reversal Engine live execution during this window"
                        )
                        bo_chk = ui.checkbox("Breakout Engine", value=block["breakout_engine"]).classes(
                            "text-xs"
                        ).props("dense").tooltip(
                            "Allow Breakout Engine live execution during this window"
                        )
                        _day_widgets[day].append({
                            "enabled": en, "start": start, "end": end, "target": target,
                            "telegram": tg_chk, "reversal_engine": re_chk, "breakout_engine": bo_chk,
                        })

    def _save():
        try:
            new_schedule = {}
            for day, rows in _day_widgets.items():
                blocks = []
                for w in rows:
                    start_val = str(w["start"].value or "00:00").strip()
                    end_val   = str(w["end"].value or "23:59").strip()
                    sched._parse_hm(start_val)  # validates HH:MM, raises on bad input
                    sched._parse_hm(end_val)
                    blocks.append({
                        "enabled": bool(w["enabled"].value),
                        "start":   start_val,
                        "end":     end_val,
                        "target":  float(w["target"].value or 0),
                        "telegram":         bool(w["telegram"].value),
                        "reversal_engine":  bool(w["reversal_engine"].value),
                        "breakout_engine":  bool(w["breakout_engine"].value),
                    })
                new_schedule[day] = blocks
        except Exception as e:
            ui.notify(f"Invalid time — use 24-hour HH:MM (e.g. 09:00): {e}", type="negative")
            return
        sched.set_trading_schedule(new_schedule)
        sched.set_trading_schedule_enabled(bool(master_chk.value))
        ui.notify(
            "Trading Schedule saved and enabled" if master_chk.value
            else "Trading Schedule saved (currently disabled — automated orders are not restricted)",
            type="positive" if master_chk.value else "info",
        )

    ui.button("Save Schedule", icon="save", on_click=_save).classes(
        "bg-blue-700 text-white px-4 py-2 mt-2"
    )
