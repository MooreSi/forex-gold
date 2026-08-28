"""The header bar: the ticker strip, account panel, badges and its refresh.

Lifted out of main_page, which was 1,101 lines inside a 1,288-line module.
This is the only seam that brings frontend/app/ under the 800-line ceiling --
see docs/todo/refactor/frontend/restructure/phase2-view-decomposition/041-*.md
for why it waited for a render test rather than being done on judgement.

The 27 names the refresh closes over are all built here, so they stay a
closure rather than becoming 27 parameters. What genuinely crosses the
boundary is small: two dialogs and the repo root go in, the Help button's
cell comes back out.
"""
import asyncio
import logging
from typing import Optional

from nicegui import ui

from backend.src.controllers import settings_controller as cfg_module
from backend.src.app import ADMIN_AVAILABLE, admin_open_fn as _admin_open_fn, get_engine
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers.system_controller import app_version as _app_version

_APP_VERSION = _app_version()

from ._shared import _CASH_REGISTER_JS, STATIC_DIR

log = logging.getLogger(__name__)


def build_header(*, power_dialog, pause_dialog, root):
    """Build the header bar and start its 2-second refresh.

    Returns the Help button's one-element cell. The button is created here but
    its handler only exists once Getting Started has been attached further down
    main_page, so the caller fills the cell in afterwards.
    """
    # Bound to the original names so the body below is unchanged from when it
    # lived in main_page.
    _power_dialog = power_dialog
    _pause_dialog = pause_dialog

    # ── Compact ticker strip ───────────────────────────────────────────────────
    _prev_bid: list[Optional[float]] = [None]
    _price_hist: list[float]         = []

    with ui.row().classes(
        "w-full items-center bg-gray-900 border-b border-gray-700 px-3 gap-0"
    ).style("height:54px; min-height:54px;"):

        # Banner / logo
        banner = STATIC_DIR / "banner.png"
        if banner.exists():
            ui.image("/static/banner.png").classes("h-10 object-contain pr-3 shrink-0")
        else:
            with ui.column().classes("pr-3 gap-0 justify-center shrink-0"):
                ui.label("FOREX Trader").classes(
                    "text-sm font-bold text-yellow-400 leading-none"
                )
                ui.label("by Simon Moore").classes(
                    "text-xs leading-none"
                ).style("color:#38bdf8")
                ui.label(f"BETA Version {_APP_VERSION}").classes(
                    "text-xs leading-none font-semibold"
                ).style("color:#4ade80; font-size:9px;")

        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")

        # Stat helper — single line: LABEL VALUE, wrapped so both share the same
        # flex baseline and font-mono metrics don't shift the value down.
        def _stat_inline(label: str, init: str = "$—", cls: str = "text-white"):
            with ui.row().classes("items-center gap-1 shrink-0 pr-3"):
                ui.label(label).classes("text-xs text-gray-500 leading-none shrink-0")
                val = ui.label(init).classes(
                    f"text-xs font-mono font-semibold leading-none {cls}"
                )
            return val

        bid_val    = _stat_inline("BID")
        ask_val    = _stat_inline("ASK")
        spread_lbl = ui.label("spr:—").classes("text-xs text-gray-500 pr-3 leading-none")
        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")
        bal_val  = _stat_inline("MT5 BAL")
        fm_lbl   = ui.label("").classes("text-xs text-gray-500 pr-3 leading-none")
        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-2")
        eq_val   = _stat_inline("EQUITY")
        eq_sub   = ui.label("").classes("text-xs pr-3 leading-none")

        # ── GitHub "Update Available" badge (2026-08-01) ─────────────────────
        # Hidden entirely when no update is available -- only ever shown once
        # a git fetch against origin/main confirms new commits. Click opens a
        # popup that says in plain English what the update changes (core_app_
        # update.summarise_changes, falling back to the commit subjects when
        # no AI provider is configured or the call fails), with the raw commit
        # list collapsed underneath; confirming pulls, reinstalls dependencies,
        # and restarts (core_app_update.py / platform_utils.restart_app, the
        # same relaunch mechanism the Power dialog uses).
        update_badge = ui.button("Update Available", icon="new_releases").props(
            "dense unelevated color=green"
        ).classes("text-xs shrink-0 ml-1 animate-pulse")
        update_badge.set_visibility(False)
        _pending_update: dict = {"commits": []}
        # Cached AI summary, keyed by the remote SHA it describes, so reopening
        # the popup doesn't pay for another LLM call for the same update.
        _update_summary_cache: dict = {"sha": "", "bullets": [], "error": ""}
        _update_summary_seq = [0]  # guards against a stale in-flight summary

        with ui.dialog() as _update_dialog, ui.card().classes(
            "bg-gray-800 p-5 rounded-lg min-w-96 max-w-lg"
        ):
            ui.label("Update Available").classes("text-base font-semibold text-white mb-1")
            _update_dialog_sub = ui.label("").classes("text-xs text-gray-400 mb-2")
            # What's in the update, in plain English (core_app_update.
            # summarise_changes). The raw commit list lives below it, collapsed.
            _update_summary_body = ui.column().classes("w-full gap-1")
            with ui.expansion("Commit list").props("dense").classes(
                "w-full text-xs text-gray-400 mt-2"
            ):
                _update_dialog_body = ui.column().classes(
                    "w-full gap-0.5 max-h-72 overflow-y-auto"
                )
            _update_dialog_status = ui.label("").classes("text-xs mt-2")

            async def _do_apply_update():
                _update_dialog_status.text = "Updating — pulling latest code, reinstalling dependencies..."
                _update_dialog_status.classes(replace="text-xs text-orange-300 mt-2")
                from backend.src.controllers import system_controller as core_app_update
                result = await core_app_update.apply_update()
                if not result["ok"]:
                    _update_dialog_status.text = f"Update failed: {result['error']}"
                    _update_dialog_status.classes(replace="text-xs text-red-400 mt-2")
                    return
                ui.notify("Update applied — restarting...", type="positive")
                from backend.src.controllers.system_controller import restart_app
                await asyncio.sleep(1)
                restart_app(root)

            with ui.row().classes("gap-2 mt-3 justify-end"):
                ui.button("Cancel", on_click=_update_dialog.close).props("flat")
                ui.button("Update Now", on_click=_do_apply_update).classes(
                    "bg-green-700 text-white"
                )

        def _update_summary_target() -> str:
            return _pending_update.get("remote_sha", "") or ""

        def _draw_update_summary(bullets: list[str], error: str) -> None:
            """Fill the summary section: the AI bullets when we have them,
            otherwise the commit subjects, which are still a truer answer to
            "what changed?" than a bare "an update is available"."""
            _update_summary_body.clear()
            commits = _pending_update.get("commits") or []
            with _update_summary_body:
                if bullets:
                    for b in bullets:
                        with ui.row().classes("items-start gap-2 no-wrap"):
                            ui.label("•").classes("text-xs text-green-400 leading-relaxed")
                            ui.label(b).classes("text-xs text-gray-200 leading-relaxed")
                    return
                for c in commits:
                    with ui.row().classes("items-start gap-2 no-wrap"):
                        ui.label("•").classes("text-xs text-green-400 leading-relaxed")
                        ui.label(c["summary"]).classes("text-xs text-gray-200 leading-relaxed")
                if not commits:
                    ui.label(
                        "The commit list for this update could not be read."
                    ).classes("text-xs text-gray-400")
                if error:
                    ui.label(f"Plain-English summary unavailable — {error}").classes(
                        "text-xs text-gray-500 italic mt-1"
                    )

        async def _load_update_summary():
            remote_sha = _update_summary_target()
            _update_summary_seq[0] += 1
            seq = _update_summary_seq[0]
            from backend.src.controllers import system_controller as core_app_update
            try:
                bullets, error = await core_app_update.summarise_changes(
                    _pending_update.get("local_sha", ""), remote_sha,
                )
            except Exception as e:  # summarising must never block updating
                bullets, error = [], f"{type(e).__name__}: {e}"
            if seq != _update_summary_seq[0]:
                return  # a newer open/check superseded this one
            _update_summary_cache.update(
                {"sha": remote_sha, "bullets": bullets, "error": error}
            )
            _draw_update_summary(bullets, error)

        async def _open_update_dialog():
            commits = _pending_update.get("commits") or []
            _update_dialog_sub.text = (
                f"{len(commits)} new commit(s) from github.com/MooreSi/forex."
                if commits else "An update is ready from github.com/MooreSi/forex."
            )

            _update_dialog_body.clear()
            with _update_dialog_body:
                for c in commits:
                    ui.label(f"{c['short_sha']}  {c['summary']}").classes(
                        "text-xs font-mono text-gray-300 leading-relaxed"
                    )

            remote_sha = _update_summary_target()
            cached = bool(remote_sha) and _update_summary_cache["sha"] == remote_sha
            if cached:
                _draw_update_summary(
                    _update_summary_cache["bullets"], _update_summary_cache["error"]
                )
            else:
                _update_summary_body.clear()
                with _update_summary_body:
                    ui.label("Summarising what's changed...").classes(
                        "text-xs text-gray-400 italic"
                    )

            _update_dialog_status.text = ""
            _update_dialog.open()
            if not cached:
                await _load_update_summary()

        update_badge.on("click", _open_update_dialog)

        async def _check_github_update():
            from backend.src.controllers import system_controller as core_app_update
            try:
                result = await core_app_update.check_for_update()
            except Exception:
                return
            _pending_update["commits"]   = result.get("commits", [])
            _pending_update["local_sha"] = result.get("local_sha", "")
            _pending_update["remote_sha"] = result.get("remote_sha", "")
            update_badge.set_visibility(bool(result.get("available")))

        ui.timer(2.0, _check_github_update, once=True)
        ui.timer(600.0, _check_github_update)  # re-check every 10 minutes

        ui.space()

        # Sparkline
        sparkline = ui.echart({
            "backgroundColor": "transparent",
            "animation": False,
            "grid": {"left": 0, "right": 0, "top": 2, "bottom": 2},
            "xAxis": {"type": "category", "show": False, "data": []},
            "yAxis": {"type": "value",    "show": False, "scale": True},
            "series": [{
                "type": "line", "data": [], "showSymbol": False,
                "lineStyle": {"color": "#00CC88", "width": 1.5},
                "areaStyle": {"color": "rgba(0,204,136,0.12)"},
            }],
        }).classes("w-20 shrink-0").style("height:28px")

        acct_lbl    = ui.label(
            "XAUUSD Gold Live"
            if cfg_module.get_config("account_env", "demo") == "live"
            else "XAUUSD Simulation"
        ).classes("text-xs text-gray-500 px-2 shrink-0")
        conn_badge  = ui.badge("MT5 —", color="grey").classes("text-xs shrink-0")
        ea_badge    = ui.badge("EA", color="grey").classes("text-xs shrink-0 ml-1")
        mode_btn = ui.button("LOCAL").props("dense outline size=sm color=amber").classes(
            "text-xs ml-1 shrink-0"
        ).style("min-height:20px; padding:0 8px;").tooltip(
            "Local/Remote sync — switch which node is actively trading"
        )
        pause_badge = ui.badge("⏸ PAUSED", color="orange").classes(
            "text-xs ml-1 shrink-0"
        ).style("display:none")
        news_badge = ui.badge("📰 NEWS EVENT", color="red").classes(
            "text-xs ml-1 shrink-0"
        ).style("display:none")

        # ── Local/Remote mode toggle ─────────────────────────────────────────
        # Replaces the old strategy badge. Switching modes runs the
        # STAND_DOWN/RESUME handshake over the sync channel (see
        # forex_trader/sync/) so exactly one node ever executes new trades.
        # This node's own sub-engines (breakout/bounce/reversal_engine) are fully
        # stopped/started as part of the switch; the Telegram reader itself
        # keeps running either way to avoid MTProto reconnect churn, but
        # SimulationEngine.open_trade() refuses every new order while stood
        # down regardless — the reader being "warm" cannot execute anything.
        _mode_switching = [False]

        def _mode_sub_engines():
            # Function-local on purpose: defers the engine imports past app
            # boot, exactly as the direct service imports here always did.
            from backend.src.controllers import engines_controller
            return engines_controller.sub_engines()

        async def _refresh_mode_btn():
            if _mode_switching[0]:
                return
            from backend.src.controllers import sync_controller as sync_ctl
            if not sync_ctl.is_connected():
                mode_btn.text = "LOCAL"
                mode_btn.props("color=grey")
                mode_btn.tooltip(
                    "No remote node connected — trading locally by default. "
                    "Configure one in Settings > Remote Node."
                )
                return
            active = settings_ctl.get_active_trader()
            if active == "local":
                mode_btn.text = "LOCAL"
                mode_btn.props("color=amber")
                mode_btn.tooltip("This machine is actively trading. Click to hand control back to the VPS.")
            else:
                mode_btn.text = "REMOTE"
                mode_btn.props("color=blue")
                mode_btn.tooltip("VPS is actively trading; this is a view-only dashboard. Click to take over.")

        async def _toggle_mode():
            from backend.src.controllers import sync_controller as sync_ctl
            if not sync_ctl.is_connected():
                ui.notify("Not connected to a remote node — configure one in "
                          "Settings > Remote Node first.", type="warning")
                return
            if _mode_switching[0]:
                return
            _mode_switching[0] = True
            mode_btn.text = "SWITCHING…"
            mode_btn.props("color=grey")
            mode_btn.disable()
            try:
                current = settings_ctl.get_active_trader()
                if current != "local":
                    # Remote -> Local: take over. Must succeed on the VPS
                    # side (its ack) before this node starts trading.
                    try:
                        ack = await sync_ctl.request_stand_down(timeout=15.0)
                    except Exception as exc:
                        ui.notify(f"VPS did not acknowledge stand-down: {exc}", type="negative")
                        return
                    settings_ctl.set_active_trader("local")
                    from backend.src.controllers import engines_controller as _engines_ctl
                    _engines_ctl.start_stopped_engines()
                    _n_open = len(ack.get("open_positions", []))
                    ui.notify(
                        f"Now trading locally. VPS stood down"
                        + (f" ({_n_open} of its position(s) still running to their own SL/TP)" if _n_open else "")
                        + ".", type="positive",
                    )
                else:
                    # Local -> Remote: any already-open local positions keep
                    # being managed by this node's own _monitor_loop (SL/TP,
                    # trailing, etc.) regardless of active_trader — that gate
                    # only blocks *new* signals from opening, it doesn't stop
                    # this node from managing trades it already has open. So
                    # switching doesn't abandon them; the user can also just
                    # close them manually in MT5/the broker terminal directly.
                    engine = get_engine()
                    open_trades = engine.get_open_trades()
                    from backend.src.controllers import engines_controller as _engines_ctl
                    _engines_ctl.stop_running_engines()
                    try:
                        await sync_ctl.request_resume(timeout=15.0)
                    except Exception as exc:
                        ui.notify(f"VPS did not acknowledge resume: {exc}", type="negative")
                        # Engines are already stopped locally; leave them stopped
                        # rather than guess whether the VPS actually resumed.
                        settings_ctl.set_active_trader("remote_vps")
                        return
                    settings_ctl.set_active_trader("remote_vps")
                    ui.notify(
                        "Control handed back to the VPS."
                        + (f" ({len(open_trades)} local position(s) still running to their own SL/TP)"
                           if open_trades else "")
                        + " This is now view-only.", type="positive",
                    )
            finally:
                _mode_switching[0] = False
                mode_btn.enable()
                await _refresh_mode_btn()

        mode_btn.on_click(_toggle_mode)

        # ── Help button — opens Getting Started (wired after tabs exist) ───────
        _help_open: list = [None]
        ui.button(
            icon="help_outline",
            on_click=lambda: _help_open[0]() if _help_open[0] else None,
        ).classes("ml-2 shrink-0").style(
            "background:transparent; color:#38bdf8; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Help — Getting Started")

        # ── Pause button ───────────────────────────────────────────────────────
        ui.button(icon="pause_circle", on_click=_pause_dialog.open).classes(
            "ml-2 shrink-0"
        ).style(
            "background:transparent; color:#fbbf24; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Pause / Resume trading")

        # ── Power button ───────────────────────────────────────────────────────
        ui.button(icon="power_settings_new", on_click=_power_dialog.open).classes(
            "ml-1 shrink-0"
        ).style(
            "background:transparent; color:#4ade80; min-width:32px; min-height:32px; "
            "width:32px; height:32px; padding:0;"
        ).tooltip("Power options — Restart / Stop")

        # ── Admin button (far right — only visible when KeyGen is present) ──────
        if ADMIN_AVAILABLE:
            ui.button(icon="admin_panel_settings", on_click=_admin_open_fn).classes(
                "ml-2 shrink-0"
            ).style(
                "background:transparent; color:#facc15; min-width:32px; min-height:32px; "
                "width:32px; height:32px; padding:0;"
            ).tooltip("Admin panel (password protected)")

    _last_profit_seq  = [None]   # None = not yet initialised; int = last seen seq
    # Initial-deposit cache: sum of all MT5 balance-credit deals.
    # Fetched once on startup then refreshed every 5 minutes.
    # Used to compute total P&L = equity - net_deposited.
    _net_deposited    = [None]   # None = not yet fetched
    _deposit_fetch_at = [0.0]
    # News event state — tracks when we entered the current news window so we
    # send exactly one Telegram alert per event, not one every 2 seconds.
    _news_in_window      = [False]   # True while inside a news blackout window
    _news_alerted_window = [None]    # window_start timestamp of the last alert sent

    async def _refresh_header():
        try:
            import time as _time
            engine = get_engine()
            tick   = await engine.get_tick()

            # Pause badge
            pause_raw = settings_ctl.get_app_config("trade_pause_until")
            is_paused = pause_raw and float(pause_raw or 0) > _time.time()
            pause_badge.style("" if is_paused else "display:none")

            # News event badge — check live calendar; send one Telegram alert on entry
            try:
                from backend.src.services.test_signal.news_filter import get_current_event as _get_news_event
                _news_ev = _get_news_event()
                if _news_ev:
                    import datetime as _dt_news
                    _resume_utc = _dt_news.datetime.fromtimestamp(
                        _news_ev["window_end"], tz=_dt_news.timezone.utc
                    ).strftime("%H:%M UTC")
                    _pause_mins = int(round(_news_ev["mins_remaining"]))
                    news_badge.text  = f"📰 NEWS EVENT — resumes {_resume_utc}"
                    news_badge.style("")
                    # Send Telegram alert once per unique event window
                    _ws = _news_ev["window_start"]
                    if not _news_in_window[0] or _news_alerted_window[0] != _ws:
                        _news_in_window[0]      = True
                        _news_alerted_window[0] = _ws
                        try:
                            from backend.src.services.telegram import alerts as _tg
                            import datetime as _dt2
                            _ev_time = _dt2.datetime.fromtimestamp(
                                _news_ev["event_ts"], tz=_dt2.timezone.utc
                            ).strftime("%H:%M UTC")
                            _mins_pre = int(round(max(0, _news_ev["mins_to_event"])))
                            _tg_msg = (
                                f"⚠️ *NEWS EVENT — Trading Paused*\n\n"
                                f"📰 *{_news_ev['title']}* ({_news_ev['currency']})\n"
                                f"🕐 Scheduled: *{_ev_time}*\n"
                                f"⏱ Paused for: *{_pause_mins} minutes*\n"
                                f"✅ Resumes at: *{_resume_utc}*\n\n"
                                f"_All signal generators continue running. "
                                f"No MT5 orders will be placed until the window clears._"
                            )
                            await _tg.send_message(_tg_msg, event_type="news_pause")
                        except Exception as _news_alert_exc:
                            log.warning("news-pause Telegram alert failed: %s", _news_alert_exc)
                else:
                    news_badge.style("display:none")
                    _news_in_window[0] = False
            except Exception:
                news_badge.style("display:none")

            if tick:
                prev = _prev_bid[0]
                bid_val.text    = f"${tick.bid:,.2f}"
                ask_val.text    = f"${tick.ask:,.2f}"
                spread_lbl.text = f"spr:{tick.spread_points:.0f}pt"
                if prev is not None:
                    chg = tick.bid - prev
                    col = "text-green-400" if chg >= 0 else "text-red-400"
                    bid_val.classes(replace=f"text-xs font-mono font-semibold {col} pr-3")
                _prev_bid[0] = tick.bid
                _price_hist.append(tick.bid)
                if len(_price_hist) > 60:
                    _price_hist.pop(0)
                sparkline.options["xAxis"]["data"]     = list(range(len(_price_hist)))
                sparkline.options["series"][0]["data"] = list(_price_hist)
                sparkline.update()

            mt5_acc = await engine.get_mt5_account()
            if mt5_acc:
                bal      = float(mt5_acc.get("balance", 0) or 0)
                eq       = float(mt5_acc.get("equity",  0) or 0)
                fm       = float(mt5_acc.get("margin_free", 0) or 0)
                bal_val.text = f"${bal:,.2f}"
                fm_lbl.text  = f"free:${fm:,.0f}"
                eq_val.text  = f"${eq:,.2f}"

                # Refresh initial deposit every 5 minutes.
                # Sum all MT5 balance-credit deals (type=2, positive profit).
                # Net deposits = total credits - total withdrawals.
                import time as _t2
                now_m = _t2.monotonic()
                if _net_deposited[0] is None or now_m - _deposit_fetch_at[0] > 300:
                    try:
                        all_deals = await engine._bridge.get_deal_history(3650)
                        credits = sum(
                            float(d.get("profit", 0))
                            for d in all_deals
                            if d.get("type") == 2 and float(d.get("profit", 0)) > 0
                        )
                        debits = sum(
                            abs(float(d.get("profit", 0)))
                            for d in all_deals
                            if d.get("type") == 2 and float(d.get("profit", 0)) < 0
                        )
                        if credits > 0:
                            _net_deposited[0] = credits - debits
                            _deposit_fetch_at[0] = now_m
                    except Exception as _dep_exc:
                        # Header P&L keeps its last value — say so in the log
                        # instead of silently showing a stale figure.
                        log.warning("net-deposit refresh failed (header P&L may be stale): %s",
                                    _dep_exc)

                if _net_deposited[0]:
                    total_pnl = eq - _net_deposited[0]
                    col = "text-green-400" if total_pnl >= 0 else "text-red-400"
                    eq_sub.text = f"P&L:${total_pnl:+.2f}"
                    eq_sub.classes(replace=f"text-xs pr-3 {col}")

            health = await engine.get_bridge_health()
            if health.get("connected"):
                if health.get("trade_allowed") is False:
                    conn_badge.props("color=orange")
                    conn_badge.text = "MT5: AutoTrading OFF"
                    conn_badge.tooltip(
                        "AutoTrading is disabled in MetaTrader 5. "
                        "Click the AutoTrading button (robot icon) in the MT5 toolbar to enable it "
                        "before placing trades."
                    )
                else:
                    conn_badge.props("color=green")
                    conn_badge.text = "MT5 Connected"
            else:
                conn_badge.props("color=red")
                conn_badge.text = "MT5 Disconnected"

            # EA badge — logic lives in ea_bridge.get_effective_ea_status()
            # (testable, and shared if any other page ever needs it) so this
            # stays a thin render step. That heartbeat/health check already
            # refreshes on its own (sync heartbeat every 3s; local EABridge
            # health computed fresh here), so no extra polling is needed.
            try:
                from backend.src.controllers import broker_controller as _ea_bridge_mod
                ea_ok, ea_scope = _ea_bridge_mod.get_effective_ea_status()
                ea_badge.props(f"color={'green' if ea_ok else 'red'}")
                ea_badge.tooltip(
                    f"EA connected on {ea_scope} — trades can be managed natively in MT5"
                    if ea_ok else
                    f"EA not connected on {ea_scope} — trades still work, "
                    "falling back to Python-managed instead of native on-tick management"
                )
            except Exception as _ea_exc:
                log.debug("EA badge refresh failed: %s", _ea_exc)

            await _refresh_mode_btn()

            cfg = cfg_module.load_config()
            env = cfg.get("account_env", "demo")
            acct_lbl.text = (
                "XAUUSD Gold Live" if env == "live" else "XAUUSD Simulation"
            )

            # Play cash register sound when a trade closes with profit.
            # On the first tick we silently snapshot the current count so the
            # sound only fires for profitable closes that happen *during* this
            # browser session, not for historical ones.
            current_profit_seq = engine._profit_sound_seq
            if _last_profit_seq[0] is None:
                _last_profit_seq[0] = current_profit_seq
            elif current_profit_seq > _last_profit_seq[0]:
                _last_profit_seq[0] = current_profit_seq
                await ui.run_javascript(_CASH_REGISTER_JS)

        except Exception as _hdr_exc:
            # One bad tick must not kill the 2s header timer, but a broken
            # header refresh showing stale balances silently is worse — log it.
            log.warning("header refresh failed (values on screen may be stale): %s", _hdr_exc)

    ui.timer(2.0, _refresh_header)

    return _help_open
