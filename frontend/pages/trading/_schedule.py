"""Trading schedule, and the strategy-comparison table cells."""
from datetime import datetime, timezone
from nicegui import ui
from backend.src.controllers import trading_controller as trading_ctl
from backend.src.controllers.trading_controller import (
    STRATEGY_NAMES,
)

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
    from backend.src.controllers import schedule_controller as sched

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
        "profit target is met trading pauses until the next window. Open a window's "
        "Channels panel to independently allow/block each Telegram channel plus "
        "Reversal Engine and Breakout Engine -- unchecking one blocks only that "
        "source's live execution for this window, the others are unaffected. Every "
        "item in the panel has its own Override dropdown, so different channels (or "
        "the two engines) can each run a different strategy or EA template within "
        "the same window, taking priority over that source's own Channel Strategy "
        "pick for as long as the window is active. Signal generation and Telegram "
        "ingestion keep running regardless -- this only blocks/redirects the final "
        "order-placement step, and only for automated (not manual) orders."
    ).classes("text-xs text-gray-500 mb-3")

    with ui.row().classes("items-center gap-2 mb-3"):
        daily_target_input = ui.number(
            "Daily Profit Target $", value=sched.get_daily_profit_target(), min=0, step=1.0,
        ).props("dense outlined").classes("w-48")
        ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
            "Cumulative profit across the WHOLE day, all windows combined. Once "
            "reached, automated trading stops for the rest of the day regardless "
            "of which window/hours would otherwise still be open.\n\n"
            "0 = disabled -- reverts to each window's own Target $ above (and if "
            "every window's target is also 0, there is no profit cap at all)."
        )

    # Strategy/EA-template options for the per-window / per-channel override
    # dropdowns -- same combined-enumeration pattern as
    # _render_channel_strategy_card's strat_opts, minus "Inherit Global"/
    # "Auto (Claude)" (those describe a per-channel fallback that doesn't
    # map onto a time window).
    from backend.src.controllers import broker_controller as _sched_et
    from backend.src.services.channels.repo import get_telegram_channel_names
    # "Auto (AI)" sits directly under "No Override" because it is the option
    # most likely to be wanted (2026-08-14). Picking it hands this channel's
    # template choice to the auto-manage layer: a backtested regime->template
    # baseline refreshed every minute, reviewed by Claude on every regime
    # change and at least every 15 minutes, and allowed to stand the channel
    # down entirely in conditions where it has no measured edge. See
    # core_auto_template and engine._auto_template_loop.
    _sched_strat_opts = {"": "— No Override —", "auto": "⚙ Auto (AI-managed)"}
    _sched_strat_opts.update(STRATEGY_NAMES)
    for _t in _sched_et.list_ea_templates():
        _sched_strat_opts[_sched_et.override_for_template(_t["name"])] = f"Template: {_t['name']}"

    _sched_channels = get_telegram_channel_names()
    _ENGINE_LABELS = {"reversal_engine": "Reversal Engine", "breakout_engine": "Breakout Engine"}

    _day_widgets: dict[str, list[dict]] = {}

    def _copy_monday_to_all():
        monday_rows = _day_widgets.get("monday", [])
        if not monday_rows:
            return
        copied = 0
        for day, rows in _day_widgets.items():
            if day == "monday":
                continue
            for src, dst in zip(monday_rows, rows):
                for key in (
                    "enabled", "start", "end", "target",
                    "reversal_engine", "breakout_engine",
                    "reversal_engine_override", "breakout_engine_override",
                    "telegram_default_enabled",
                ):
                    dst[key].value = src[key].value
                for ch, src_cw in src["telegram_channels"].items():
                    dst_cw = dst["telegram_channels"].get(ch)
                    if dst_cw is None:
                        continue
                    dst_cw["enabled"].value = src_cw["enabled"].value
                    dst_cw["strategy_override"].value = src_cw["strategy_override"].value
            copied += 1
        ui.notify(
            f"Copied Monday's schedule to {copied} other days — click Save Schedule to persist",
            type="positive",
        )

    with ui.column().classes("w-full gap-2"):
        for day in sched.DAY_NAMES:
            with ui.card().classes("w-full bg-gray-800 p-3 rounded-lg"):
                with ui.row().classes("items-center gap-2 mb-1"):
                    ui.label(day.title()).classes("font-bold text-yellow-300 text-sm")
                    if day == "monday":
                        ui.button(
                            "Copy to All", icon="content_copy", on_click=_copy_monday_to_all,
                        ).props("dense flat color=blue size=sm").classes("text-xs").tooltip(
                            "Copy every one of Monday's windows (times, targets, engines, "
                            "channels) to the other 6 days"
                        )
                blocks = schedule[day]
                _day_widgets[day] = []
                for i, block in enumerate(blocks):
                    with ui.column().classes(
                        "w-full gap-1 pb-2 mb-1 border-b border-gray-700"
                    ):
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

                        # ── Channels panel: Telegram channels + the two ────
                        # internal engines, each with its own enable toggle
                        # and its own strategy Override.
                        _tg_cfg = block.get("telegram_channels", {})
                        _tg_default = block.get("telegram_default_enabled", True)
                        _tg_on = sum(
                            1 for ch in _sched_channels
                            if _tg_cfg.get(ch, {}).get("enabled", _tg_default)
                        )
                        _engine_on = sum(1 for k in _ENGINE_LABELS if block.get(k, True))
                        _total_on = _tg_on + _engine_on
                        _total_items = len(_sched_channels) + len(_ENGINE_LABELS)
                        _exp_label = f"Channels ({_total_on}/{_total_items} enabled)"
                        with ui.expansion(_exp_label, icon="tune").classes(
                            "w-full text-xs text-gray-400 bg-gray-900 rounded"
                        ).props("dense"):
                            _chan_widgets: dict[str, dict] = {}
                            with ui.column().classes("w-full gap-1 pl-2 pt-1 pb-1"):
                                default_chk = ui.checkbox(
                                    "New/unlisted channels default to enabled",
                                    value=_tg_default,
                                ).props("dense").classes("text-xs text-gray-500").tooltip(
                                    "Applies to any Telegram channel not explicitly set "
                                    "below -- including one added after this schedule "
                                    "was saved."
                                )
                                if _sched_channels:
                                    ui.separator().classes("my-1 bg-gray-700")
                                for ch in _sched_channels:
                                    _ch_cfg = _tg_cfg.get(ch, {})
                                    with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                        c_chk = ui.checkbox(
                                            ch, value=bool(_ch_cfg.get("enabled", _tg_default)),
                                        ).props("dense").classes("text-xs w-56").tooltip(
                                            f"Allow automated trades from {ch} during this window"
                                        )
                                        c_sel = ui.select(
                                            _sched_strat_opts,
                                            value=_ch_cfg.get("strategy_override") or "",
                                            label="Override",
                                        ).props("dense outlined").classes("w-48").tooltip(
                                            f"Force {ch} onto this strategy or EA template "
                                            "while this window is active, overriding its "
                                            "own Channel Strategy pick. No Override = "
                                            "normal per-channel resolution. "
                                            "Auto (AI-managed) = the template is chosen "
                                            "from live market conditions and may stand "
                                            "this channel down entirely."
                                        )
                                    _chan_widgets[ch] = {"enabled": c_chk, "strategy_override": c_sel}

                                # Internal signal generators -- same row shape
                                # as a channel, each with its own Override so
                                # Reversal Engine and Breakout Engine can run
                                # different strategies in the same window.
                                ui.separator().classes("my-1 bg-gray-700")
                                ui.label("Internal Signal Generators").classes(
                                    "text-xs text-gray-500 uppercase tracking-wide"
                                )
                                with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                    re_chk = ui.checkbox(
                                        "Reversal Engine", value=block["reversal_engine"],
                                    ).props("dense").classes("text-xs w-56").tooltip(
                                        "Allow Reversal Engine live execution during this window"
                                    )
                                    re_ov = ui.select(
                                        _sched_strat_opts,
                                        value=block.get("reversal_engine_override") or "",
                                        label="Override",
                                    ).props("dense outlined").classes("w-48").tooltip(
                                        "Force Reversal Engine onto this strategy or EA "
                                        "template while this window is active, overriding "
                                        "its own Channel Strategy pick. No Override = "
                                        "normal resolution."
                                    )
                                with ui.row().classes("items-center gap-2 flex-wrap w-full"):
                                    bo_chk = ui.checkbox(
                                        "Breakout Engine", value=block["breakout_engine"],
                                    ).props("dense").classes("text-xs w-56").tooltip(
                                        "Allow Breakout Engine live execution during this window"
                                    )
                                    bo_ov = ui.select(
                                        _sched_strat_opts,
                                        value=block.get("breakout_engine_override") or "",
                                        label="Override",
                                    ).props("dense outlined").classes("w-48").tooltip(
                                        "Force Breakout Engine onto this strategy or EA "
                                        "template while this window is active, overriding "
                                        "its own Channel Strategy pick. No Override = "
                                        "normal resolution."
                                    )

                        _day_widgets[day].append({
                            "enabled": en, "start": start, "end": end, "target": target,
                            "reversal_engine": re_chk, "breakout_engine": bo_chk,
                            "reversal_engine_override": re_ov,
                            "breakout_engine_override": bo_ov,
                            "telegram_default_enabled": default_chk,
                            "telegram_channels": _chan_widgets,
                        })

    def _save():
        try:
            new_schedule = {}
            for day, rows in _day_widgets.items():
                blocks = []
                for w in rows:
                    start_val = str(w["start"].value or "00:00").strip()
                    end_val   = str(w["end"].value or "23:59").strip()
                    sched.parse_hm(start_val)  # validates HH:MM, raises on bad input
                    sched.parse_hm(end_val)

                    def _sel_val(widget):
                        v = widget.value
                        return v.get("value") if isinstance(v, dict) else v

                    _tg_channels = {}
                    for ch, cw in w["telegram_channels"].items():
                        _tg_channels[ch] = {
                            "enabled": bool(cw["enabled"].value),
                            "strategy_override": _sel_val(cw["strategy_override"]) or "",
                        }
                    blocks.append({
                        "enabled": bool(w["enabled"].value),
                        "start":   start_val,
                        "end":     end_val,
                        "target":  float(w["target"].value or 0),
                        "reversal_engine":  bool(w["reversal_engine"].value),
                        "breakout_engine":  bool(w["breakout_engine"].value),
                        "reversal_engine_override": _sel_val(w["reversal_engine_override"]) or "",
                        "breakout_engine_override": _sel_val(w["breakout_engine_override"]) or "",
                        "telegram_default_enabled": bool(w["telegram_default_enabled"].value),
                        "telegram_channels": _tg_channels,
                    })
                new_schedule[day] = blocks
        except Exception as e:
            ui.notify(f"Invalid time — use 24-hour HH:MM (e.g. 09:00): {e}", type="negative")
            return
        sched.set_trading_schedule(new_schedule)
        sched.set_trading_schedule_enabled(bool(master_chk.value))
        sched.set_daily_profit_target(float(daily_target_input.value or 0))
        ui.notify(
            "Trading Schedule saved and enabled" if master_chk.value
            else "Trading Schedule saved (currently disabled — automated orders are not restricted)",
            type="positive" if master_chk.value else "info",
        )

    ui.button("Save Schedule", icon="save", on_click=_save).classes(
        "bg-blue-700 text-white px-4 py-2 mt-2"
    )
