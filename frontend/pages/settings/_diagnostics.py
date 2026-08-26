"""System diagnostics tab: health checks, log tail, sleep prevention and
the autostart controls."""
import asyncio
import sys

from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl
from backend.src.services.positions import core_autostart as _autostart
from ._shared import _pu

# Holds Popen (macOS) or _WindowsSleepGuard (Windows). Lives here rather than
# in the package __init__ because _render_diagnostics is the only reader and
# the only writer: split the two and each module silently gets its own copy.
_caffeinate_proc = None


def _render_diagnostics(engine):
    import re as _re
    import time as _time_mod
    from pathlib import Path as _Path

    diag_container    = ui.column().classes("w-full")
    live_diag_card    = ui.card().classes("w-full bg-gray-900 border border-gray-700 rounded-lg p-3 hidden")
    _live_diag_active = [False]
    _live_diag_timer  = [None]

    async def _render_live_lines():
        live_diag_card.clear()
        with live_diag_card:
            with ui.row().classes("items-center justify-between mb-2"):
                ui.label("Live Diagnostics — since last restart").classes(
                    "text-purple-300 font-bold text-sm"
                )
                ui.label("Auto-refreshes every 5 seconds  ·  Errors / Warnings / Events only").classes(
                    "text-gray-500 text-xs"
                )

            # Offloaded to the DB worker thread — this reads and parses the
            # entire log file (can run tens of MB) every 5 seconds; doing that
            # synchronously on the event loop blocked the whole app for as
            # long as this took, same class of bug as the sqlite3.connect()
            # sites fixed earlier.
            lines = await settings_ctl.live_log_lines()
            if not lines:
                ui.label(
                    "No events recorded yet since last restart. Start the engine and generate some activity."
                ).classes("text-gray-500 text-sm font-mono")
                return

            _LEVEL_CLS = {
                "error":   "text-red-400",
                "warning": "text-yellow-300",
                "event":   "text-gray-300",
            }
            with ui.scroll_area().classes("w-full").style("max-height:500px"):
                with ui.column().classes("w-full gap-0"):
                    for level, ln in lines:
                        cls = _LEVEL_CLS.get(level, "text-gray-400")
                        ui.label(ln).classes(
                            f"font-mono text-xs {cls} leading-relaxed whitespace-pre-wrap break-all"
                        )

    def _toggle_live_diag():
        _live_diag_active[0] = not _live_diag_active[0]
        if _live_diag_active[0]:
            live_diag_card.classes(remove="hidden")
            asyncio.create_task(_render_live_lines())
            if _live_diag_timer[0] is None:
                from nicegui import ui as _ui
                _live_diag_timer[0] = _ui.timer(5.0, _render_live_lines)
        else:
            live_diag_card.classes(add="hidden")
            if _live_diag_timer[0]:
                _live_diag_timer[0].cancel()
                _live_diag_timer[0] = None

    async def run_diag():
        import platform as _platform
        import sys as _sys
        import time as _t

        diag_container.clear()
        with diag_container:
            ui.spinner()

        # ── Timed bridge calls ─────────────────────────────────────────────
        t0 = _t.monotonic()
        try:
            health  = await engine.get_bridge_health()
        except Exception as e:
            diag_container.clear()
            with diag_container:
                ui.label(f"Diagnostics error: {e}").classes("text-red-400")
            return
        health_ms = round((_t.monotonic() - t0) * 1000)

        t1 = _t.monotonic()
        try:
            tick    = await engine.get_tick()
        except Exception:
            tick = None
        tick_ms = round((_t.monotonic() - t1) * 1000)

        t2 = _t.monotonic()
        try:
            mt5_acc = await engine.get_mt5_account()
        except Exception:
            mt5_acc = None
        acc_ms = round((_t.monotonic() - t2) * 1000)

        # ── Telegram → execution latency from DB ───────────────────────────
        tg_latency: dict = {}
        try:
            from backend.src.config import DATA_DIR as _ddir, load as _cfg_load
            _env = _cfg_load().get("account_env", "demo")
            _db_path = str(_ddir / f"forex_trader_{_env}.db")
            try:
                # parsed_at = when signal was received by the app
                # open_time = when the trade was actually opened in MT5
                _rows = settings_ctl.fetch_signal_execution_lags(_db_path)
                lags = [float(r["lag_s"]) for r in _rows if r["lag_s"] is not None]
                if lags:
                    lags_sorted = sorted(lags)
                    n = len(lags_sorted)
                    tg_latency = {
                        "count":  n,
                        "min_s":  round(lags_sorted[0], 2),
                        "avg_s":  round(sum(lags) / n, 2),
                        "p90_s":  round(lags_sorted[int(n * 0.9)], 2),
                        "max_s":  round(lags_sorted[-1], 2),
                    }
            finally:
                pass
        except Exception:
            pass

        diag_container.clear()
        with diag_container:
            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
                ui.label("MT5 Bridge").classes("font-bold text-yellow-300 mb-2")
                bridge_ok     = health.get("connected", False)
                trade_allowed = health.get("trade_allowed")
                with ui.row().classes("gap-2 flex-wrap items-center"):
                    ui.badge(
                        "Connected" if bridge_ok else "Disconnected",
                        color="green" if bridge_ok else "red",
                    )
                    if bridge_ok:
                        if trade_allowed is False:
                            ui.badge("AutoTrading OFF", color="orange")
                            ui.label(
                                "AutoTrading is disabled in MetaTrader 5. "
                                "Click the AutoTrading button (robot icon) in the MT5 toolbar "
                                "to allow the bridge to place orders."
                            ).classes("text-orange-300 text-xs mt-1 w-full")
                        elif trade_allowed is True:
                            ui.badge("AutoTrading ON", color="green")

                    from backend.src.services.broker import ea_bridge as _ea_bridge_mod
                    _ea_ok, _ea_scope = _ea_bridge_mod.get_effective_ea_status()
                    ui.badge(
                        f"EA {'Connected' if _ea_ok else 'Not Connected'}"
                        + ("" if _ea_scope == "this node" else f" ({_ea_scope})"),
                        color="green" if _ea_ok else "red",
                    )
                if health.get("last_error"):
                    ui.label(f"Last error: {health['last_error']}").classes("text-red-300 text-sm mt-1")
                if not _ea_ok:
                    if _ea_scope == "this node":
                        _ea = _ea_bridge_mod.get_instance()
                        _ea_hint = (
                            "No MQL5 EA has ever connected this session."
                            if _ea is None or _ea._last_seen == 0 else
                            f"EA connection lost {round(_time_mod.time() - _ea._last_seen)}s ago."
                        )
                    else:
                        _ea_hint = f"The {_ea_scope} (active trader) has no EA connected."
                    ui.label(
                        f"{_ea_hint} Trades will still be managed by the Python bridge -- "
                        "this only affects EA-native on-tick management. If unexpected, check "
                        "that ForexTraderBridge is attached to the chart with AutoTrading on."
                    ).classes("text-gray-400 text-xs mt-1 w-full")

            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                ui.label("Market Data").classes("font-bold text-yellow-300 mb-2")
                if tick:
                    ui.label(
                        f"Bid: {tick.bid}  Ask: {tick.ask}  Spread: {tick.spread_points:.0f}pt"
                    ).classes("text-green-300 font-mono text-sm")
                    ui.label(f"Source: {tick.source}").classes("text-gray-400 text-xs")
                else:
                    ui.label("No tick data").classes("text-red-400")

            if mt5_acc:
                with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                    ui.label("MT5 Account").classes("font-bold text-yellow-300 mb-2")
                    ui.label(
                        f"Balance:  ${float(mt5_acc.get('balance', 0)):,.2f}"
                    ).classes("text-gray-200 font-mono text-sm")
                    ui.label(
                        f"Equity:   ${float(mt5_acc.get('equity', 0)):,.2f}"
                    ).classes("text-gray-200 font-mono text-sm")
                    ui.label(f"Server:   {mt5_acc.get('server', '?')}").classes(
                        "text-gray-400 text-xs"
                    )
                    ui.label(f"Is demo:  {mt5_acc.get('is_demo', '?')}").classes(
                        "text-gray-400 text-xs"
                    )

            # MT5 history import
            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                ui.label("MT5 History Sync").classes("font-bold text-yellow-300 mb-2")
                ui.label(
                    "Pull closed positions from MT5 bridge and import them into the local database. "
                    "Skips positions already recorded."
                ).classes("text-xs text-gray-400 mb-2")
                sync_result = ui.label("").classes("text-sm text-gray-300")
                days_inp = ui.number("Days to look back", value=90, min=1, max=365, step=1).classes(
                    "w-40"
                )

                async def do_sync():
                    sync_result.text = "Syncing..."
                    try:
                        result = await engine.import_mt5_history(int(days_inp.value))
                        sync_result.text = (
                            f"Done — imported {result['imported']}, "
                            f"skipped {result['skipped']}."
                            + (f" {result.get('error', '')}" if result.get("error") else "")
                        )
                        ui.notify(
                            f"Sync complete: {result['imported']} imported",
                            type="positive",
                        )
                    except Exception as e:
                        sync_result.text = f"Error: {e}"
                        ui.notify(str(e), type="negative")

                ui.button("Import MT5 History", on_click=do_sync).classes(
                    "bg-blue-700 text-white px-4 py-2 mt-2"
                )
                sync_result

            # ── Latency Report ───────────────────────────────────────────────
            _is_mac  = _sys.platform == "darwin"
            _is_wine = _is_mac  # MT5 runs via Wine/CrossOver on Mac
            _plat_lbl = (
                f"macOS {_platform.mac_ver()[0]} — MT5 via Wine/CrossOver"
                if _is_mac else
                f"Windows {_platform.version()}"
            )
            _bridge_url = "localhost:9000"

            with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
                ui.label("Latency Report").classes("font-bold text-yellow-300 mb-2")
                ui.label(_plat_lbl).classes("text-xs text-gray-500 mb-3 italic")

                # ── Path 1: App → Bridge → MT5 → Vantage ────────────────────
                ui.label("Path 1 — App → Bridge → MT5 → Vantage").classes(
                    "text-xs font-semibold text-blue-300 uppercase mb-1"
                )
                with ui.element("div").style(
                    "display:grid; grid-template-columns:repeat(3,1fr); gap:8px;"
                ):
                    _steps_1 = [
                        ("App → Bridge\n(HTTP /health)", f"{health_ms} ms",
                         "text-green-400" if health_ms < 50 else "text-yellow-300" if health_ms < 200 else "text-red-400"),
                        ("App → Bridge\n(HTTP /tick)", f"{tick_ms} ms",
                         "text-green-400" if tick_ms < 50 else "text-yellow-300" if tick_ms < 200 else "text-red-400"),
                        ("App → Bridge\n(HTTP /account)", f"{acc_ms} ms",
                         "text-green-400" if acc_ms < 50 else "text-yellow-300" if acc_ms < 200 else "text-red-400"),
                    ]
                    for lbl, val, cls in _steps_1:
                        with ui.card().classes("bg-gray-900 rounded p-2 text-center"):
                            ui.label(val).classes(f"text-sm font-bold {cls}")
                            ui.label(lbl).classes("text-xs text-gray-500 whitespace-pre-line")

                _total_bridge = health_ms
                if _is_wine:
                    _wine_est = "~5–20 ms (Wine IPC overhead)"
                    ui.label(
                        f"Wine/CrossOver adds IPC overhead between the bridge and MT5 "
                        f"({_wine_est}). Total app→MT5 round-trip is approximately "
                        f"{_total_bridge + 15} ms under typical conditions."
                    ).classes("text-xs text-gray-400 italic mt-1 mb-3")
                else:
                    ui.label(
                        f"Total app→bridge round-trip: {_total_bridge} ms (direct Windows, no Wine)."
                    ).classes("text-xs text-gray-400 italic mt-1 mb-3")

                # ── Path 2: Telegram → App → Bridge → MT5 → Vantage ─────────
                ui.label("Path 2 — Telegram signal → Trade execution").classes(
                    "text-xs font-semibold text-purple-300 uppercase mb-1"
                )
                if tg_latency:
                    with ui.element("div").style(
                        "display:grid; grid-template-columns:repeat(5,1fr); gap:8px;"
                    ):
                        for lbl, val_s, hint_cls in [
                            ("Fastest\nexecution", f"{tg_latency['min_s']}s", "text-green-400"),
                            ("Average\n(end-to-end)", f"{tg_latency['avg_s']}s", "text-yellow-300"),
                            ("P90\nlatency", f"{tg_latency['p90_s']}s", "text-orange-300"),
                            ("Slowest\nrecorded", f"{tg_latency['max_s']}s", "text-red-400"),
                            ("Sample\nsize", str(tg_latency['count']), "text-gray-300"),
                        ]:
                            with ui.card().classes("bg-gray-900 rounded p-2 text-center"):
                                ui.label(val_s).classes(f"text-sm font-bold {hint_cls}")
                                ui.label(lbl).classes("text-xs text-gray-500 whitespace-pre-line")

                    ui.label(
                        "Measured from Telegram signal parsed_at to MT5 open_time. "
                        "Includes: Telegram poll lag → signal parse → app processing → "
                        f"HTTP bridge call ({health_ms} ms typical)"
                        + (" → Wine/CrossOver IPC → MT5 → Vantage order routing." if _is_wine
                           else " → MT5 → Vantage order routing.")
                    ).classes("text-xs text-gray-400 italic mt-2")
                    if _is_wine:
                        ui.label(
                            "On macOS with CrossOver, Wine IPC adds latency between the Python bridge "
                            "and MetaTrader 5. To minimise this, keep CrossOver and MT5 running "
                            "continuously and avoid cold starts."
                        ).classes("text-xs text-orange-300 italic mt-1")
                else:
                    ui.label(
                        "No Telegram signal → trade execution pairs found in the database. "
                        "Latency will be calculated once live signals are received and traded."
                    ).classes("text-xs text-gray-500 italic")

    export_lbl = ui.label("").classes("text-sm mt-1")

    async def export_logs():
        import base64 as _b64
        import platform as _platform
        import sys as _sys
        import time as _time
        from datetime import datetime as _dt
        from pathlib import Path as _Path
        from backend.src.controllers import settings_controller as _db_ctl
        import httpx as _httpx

        DAYS       = 5
        MAX_BYTES  = 25 * 1024 * 1024   # 25 MB raw → ~33 MB base64 → well under Resend 40 MB cap
        TO = "simon.moore@outlook.com"

        # ── Polling endpoints that produce only noise ─────────────────────────
        # These are httpx GET lines that fire every few seconds continuously.
        _POLL_DROPS = (
            "/tick/XAUUSD",
            "/candles/XAUUSD",
            "/candles_symbol/",
            "localhost:9000/positions ",
            "localhost:9000/account ",
            "localhost:9000/health",
            "/history?days=",
            "/history/position/",
            "getUpdates",
        )

        def _keep_line(ln: str) -> bool:
            """Return True only for log lines worth including in a troubleshooting export."""
            # Drop all DEBUG lines
            if " DEBUG " in ln:
                return False
            # Drop httpx GET polling lines — these are background heartbeats
            if "INFO httpx" in ln and "HTTP Request: GET" in ln:
                for _drop in _POLL_DROPS:
                    if _drop in ln:
                        return False
            return True

        export_lbl.text = "Reading and filtering logs..."
        export_lbl.classes(replace="text-sm mt-1 text-gray-400")

        try:
            # ── Collect log files (current + rotated backups) ─────────────────
            from backend.src.config import DATA_DIR as _log_data_dir
            log_base = _Path(_log_data_dir) / "forex_trader.log"
            if not log_base.exists():
                export_lbl.text = "No log file found — restart the app first to begin writing logs."
                export_lbl.classes(replace="text-sm mt-1 text-orange-400")
                return

            # TimedRotatingFileHandler names backups forex_trader.log.YYYY-MM-DD.
            # Ascending sort gives chronological order (oldest date first),
            # then append the current (today's) file at the end.
            candidates = sorted(
                log_base.parent.glob("forex_trader.log.2*"),
            ) + [log_base]

            cutoff    = _time.time() - DAYS * 86400
            raw_total = 0       # total lines seen (before filter)
            lines     = []      # filtered lines to include

            for path in candidates:
                try:
                    with path.open("r", encoding="utf-8", errors="replace") as fh:
                        for line in fh:
                            stripped = line.rstrip()
                            if not stripped:
                                continue
                            raw_total += 1
                            # Date-range filter
                            try:
                                ts = _dt.strptime(stripped[:23], "%Y-%m-%d %H:%M:%S,%f").timestamp()
                                if ts < cutoff:
                                    continue
                            except Exception:
                                pass  # non-timestamped lines (tracebacks etc) — keep
                            if _keep_line(stripped):
                                lines.append(stripped)
                except Exception:
                    pass  # skip unreadable rotation files

            if not lines:
                export_lbl.text = (
                    f"No meaningful log entries in the last {DAYS} days "
                    f"({raw_total:,} lines scanned — all were routine polling)."
                )
                export_lbl.classes(replace="text-sm mt-1 text-orange-400")
                return

            # ── Deduplicate consecutive repeated lines ────────────────────────
            # httpx POST /modify fires every 5 s while a trade is live — collapse those.
            deduped: list[str] = []
            prev_body  = None
            repeat_cnt = 0
            for ln in lines:
                body = ln[24:] if len(ln) > 24 else ln   # strip timestamp
                if body == prev_body:
                    repeat_cnt += 1
                else:
                    if repeat_cnt > 0:
                        deduped.append(f"        ... repeated {repeat_cnt} more time(s)")
                    deduped.append(ln)
                    prev_body  = body
                    repeat_cnt = 0
            if repeat_cnt > 0:
                deduped.append(f"        ... repeated {repeat_cnt} more time(s)")

            filtered_total = len(deduped)
            now_str = _dt.now().strftime("%Y-%m-%d %H:%M")
            fname   = f"forex_trader_logs_{_dt.now().strftime('%Y%m%d_%H%M')}.txt"

            # ── Gather user and system info ────────────────────────────────────
            try:
                from backend.src.config.licence import store as _lic_store
                from backend.src.config.licence.fingerprint import get_fingerprint as _fp
                _lic_data    = _lic_store.load() or {}
                _lic_email   = _lic_data.get("email", "—")
                _lic_type    = _lic_data.get("licence_type", "—")
                _lic_expiry  = _lic_data.get("expiry_date", "—")
                _machine_id  = _fp()
            except Exception:
                _lic_email = _lic_type = _lic_expiry = _machine_id = "—"

            try:
                from backend.src.utils.version_history import __version__ as _app_ver
            except Exception:
                _app_ver = "—"

            _env_label = (
                settings_ctl.get_app_config("account_env") or "demo"
            ).upper()
            _py_ver  = _sys.version.split()[0]
            _os_info = f"{_platform.system()} {_platform.release()} ({_platform.machine()})"

            _sys_header = (
                f"FOREX Trader — Filtered Log Export\n"
                f"{'=' * 80}\n"
                f"Generated  : {now_str}\n"
                f"Period     : last {DAYS} days\n"
                f"Raw lines  : {raw_total:,}  (scanned)\n"
                f"Kept lines : {filtered_total:,}  (errors, warnings, app events)\n"
                f"Dropped    : {raw_total - filtered_total:,}  (DEBUG + polling noise)\n"
                f"\n"
                f"-- User Info --\n"
                f"Email      : {_lic_email}\n"
                f"Machine ID : {_machine_id}\n"
                f"Licence    : {_lic_type}  (expires: {_lic_expiry})\n"
                f"\n"
                f"-- System Info --\n"
                f"App version: {_app_ver}\n"
                f"Environment: {_env_label}\n"
                f"Python     : {_py_ver}\n"
                f"OS         : {_os_info}\n"
                f"{'=' * 80}\n\n"
            )

            # ── Build attachment with hard 25 MB cap ──────────────────────────
            body_text = "\n".join(deduped)
            body_enc  = body_text.encode("utf-8")
            header_enc = _sys_header.encode("utf-8")
            truncated  = False

            if len(header_enc) + len(body_enc) > MAX_BYTES:
                # Keep the most recent lines that fit (newest are at the end)
                available = MAX_BYTES - len(header_enc) - 200  # 200 b margin
                tail_bytes = body_enc[-available:]
                # Realign to the next newline boundary so we don't split a line
                nl_idx = tail_bytes.find(b"\n")
                if nl_idx != -1:
                    tail_bytes = tail_bytes[nl_idx + 1:]
                body_text = (
                    "[OLDEST ENTRIES TRUNCATED — file exceeded 25 MB limit]\n\n"
                    + tail_bytes.decode("utf-8", errors="replace")
                )
                truncated = True

            attachment_bytes = (header_enc + body_text.encode("utf-8"))
            attachment_b64   = _b64.b64encode(attachment_bytes).decode()

            # ── HTML email body ────────────────────────────────────────────────
            _trunc_note = (
                "<p style='color:#f87171;font-size:13px;margin-top:8px;'>"
                "Note: attachment was truncated to fit the 25 MB limit — oldest entries removed."
                "</p>"
            ) if truncated else ""
            size_kb = round(len(attachment_bytes) / 1024)
            html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"></head>
<body style="margin:0;padding:24px;background:#111827;font-family:Arial,sans-serif;color:#e5e7eb;">
<h2 style="color:#f59e0b;margin-bottom:6px;">FOREX Trader &mdash; Filtered Log Export</h2>
<p style="color:#9ca3af;font-size:13px;">
  {now_str} &nbsp;&bull;&nbsp; v{_app_ver}
  &nbsp;&bull;&nbsp; Last {DAYS} days
  &nbsp;&bull;&nbsp; {filtered_total:,} kept / {raw_total:,} scanned
  &nbsp;&bull;&nbsp; {size_kb} KB
</p>
{_trunc_note}
<table style="border-collapse:collapse;margin-top:16px;font-size:13px;">
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Email</td>
      <td style="color:#e5e7eb">{_lic_email}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Machine ID</td>
      <td style="color:#e5e7eb;font-family:monospace">{_machine_id}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Licence</td>
      <td style="color:#e5e7eb">{_lic_type} &mdash; expires {_lic_expiry}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Environment</td>
      <td style="color:#e5e7eb">{_env_label}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">App version</td>
      <td style="color:#e5e7eb">{_app_ver}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">Python</td>
      <td style="color:#e5e7eb">{_py_ver}</td></tr>
  <tr><td style="color:#9ca3af;padding:2px 12px 2px 0">OS</td>
      <td style="color:#e5e7eb">{_os_info}</td></tr>
</table>
<p style="color:#d1d5db;font-size:13px;margin-top:16px;">
  Filtered log attached as <strong>{fname}</strong>.<br>
  <span style="color:#6b7280;font-size:12px;">
    Dropped: DEBUG lines, tick polling, candle polling, Telegram getUpdates, position checks.
    Kept: errors, warnings, trades, signals, bridge events, DPM actions, auth events.
  </span>
</p>
</body></html>"""

            # ── Send via Resend with attachment ───────────────────────────────
            ecfg    = _db_ctl.get_email_config()
            api_key = (ecfg.get("resend_api_key") or "").strip()
            if not api_key:
                export_lbl.text = "No Resend API key configured — set it in Settings → Email Reports."
                export_lbl.classes(replace="text-sm mt-1 text-orange-400")
                return

            payload = {
                "from":    "FOREX Trader <onboarding@resend.dev>",
                "to":      [TO],
                "subject": f"FOREX Trader Logs — {now_str}",
                "html":    html,
                "attachments": [
                    {"filename": fname, "content": attachment_b64},
                ],
            }

            export_lbl.text = (
                f"Sending {filtered_total:,} filtered lines "
                f"({size_kb} KB) to {TO}..."
            )
            async with _httpx.AsyncClient(timeout=30) as client:
                r = await client.post(
                    "https://api.resend.com/emails",
                    json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            if r.status_code in (200, 201):
                export_lbl.text = (
                    f"Sent — {filtered_total:,} kept / {raw_total:,} scanned "
                    f"({size_kb} KB)."
                    + (" [truncated]" if truncated else "")
                )
                export_lbl.classes(replace="text-sm mt-1 text-green-400")
                ui.notify("Log export sent!", type="positive")
            else:
                export_lbl.text = f"Send failed (HTTP {r.status_code}): {r.text[:200]}"
                export_lbl.classes(replace="text-sm mt-1 text-red-400")

        except Exception as exc:
            export_lbl.text = f"Error: {exc}"
            export_lbl.classes(replace="text-sm mt-1 text-red-400")

    # ── Prevent-sleep toggle ────────────────────────────────────────────────
    global _caffeinate_proc

    def _sleep_is_active() -> bool:
        return _pu.is_preventing_sleep(_caffeinate_proc)

    def _update_sleep_btn(btn):
        if _sleep_is_active():
            btn.text = "Prevent Sleep: ON"
            btn.props("color=green")
        else:
            btn.text = "Prevent Sleep: OFF"
            btn.props("color=grey")

    def _toggle_sleep(btn):
        global _caffeinate_proc
        if _sleep_is_active():
            _pu.stop_prevent_sleep(_caffeinate_proc)
            _caffeinate_proc = None
            ui.notify("Sleep prevention disabled", type="info", position="top")
        else:
            try:
                _caffeinate_proc = _pu.start_prevent_sleep()
                msg = (
                    "Sleep prevention enabled — Windows will stay awake"
                    if sys.platform == "win32"
                    else "Sleep prevention enabled — Mac will stay awake"
                )
                ui.notify(msg, type="positive", position="top")
            except Exception as exc:
                ui.notify(f"Failed to enable sleep prevention: {exc}", type="negative", position="top")
        _update_sleep_btn(btn)

    _os_label = "Windows" if sys.platform == "win32" else "Mac"
    sleep_btn = ui.button(
        "Prevent Sleep: OFF",
        icon="bedtime_off",
        on_click=lambda: _toggle_sleep(sleep_btn),
    ).classes("px-4 py-2").tooltip(
        f"Keep the {_os_label} awake so no trade is missed. "
        "The screen can still turn off. "
        "Automatically releases when the app exits."
    )
    _update_sleep_btn(sleep_btn)

    # ── Auto-restart watchdog toggle ────────────────────────────────────────
    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("w-full items-center justify-between"):
            auto_restart_sw = ui.switch(
                "Auto-Restart if the app stops",
                value=(settings_ctl.get_app_config("auto_restart_enabled") == "1"),
            ).classes("text-blue-300 font-bold")
            ui.icon("restart_alt", size="sm").classes("text-blue-400")

        _autostart_lbl = ui.label("").classes("text-xs mt-1 text-gray-500")

        with ui.expansion(
            "How does Auto-Restart work?", icon="info_outline"
        ).classes("w-full text-sm"):
            ui.markdown(
                f"When **ON**, {_os_label} checks every "
                f"{_autostart.CHECK_INTERVAL_SECS // 60} minutes that the app is "
                "still serving on its port, and starts it again if it is not. "
                "It also runs that check at login/boot, so the app comes back by "
                "itself after a reboot.\n\n"
                + (
                    "Registered as a **LaunchAgent** (`launchctl`) under your user "
                    "account.\n\n"
                    if sys.platform == "darwin"
                    else "Registered as a **Scheduled Task** under your user account.\n\n"
                )
                + "Stopping the app deliberately still stops it — "
                "`FOREX Stop.command` / `Stop FOREX.bat` pause the watchdog, and "
                "starting the app again re-arms it. Restarting from inside the app "
                "is unaffected.\n\n"
                "When **OFF**, nothing restarts the app and it stays down until "
                "started by hand."
            ).classes("text-gray-300")

        def _refresh_autostart_lbl():
            if not _autostart.is_supported():
                _autostart_lbl.text = f"Not supported on this platform ({sys.platform})."
                _autostart_lbl.classes(replace="text-xs mt-1 text-gray-500")
                return
            if not auto_restart_sw.value:
                _autostart_lbl.text = "Off — nothing will restart the app if it stops."
                _autostart_lbl.classes(replace="text-xs mt-1 text-gray-500")
            elif _autostart.is_installed():
                _autostart_lbl.text = (
                    f"Active — checking every "
                    f"{_autostart.CHECK_INTERVAL_SECS // 60} min."
                    + ("" if _autostart.is_armed() else " Currently paused (app stopped).")
                )
                _autostart_lbl.classes(replace="text-xs mt-1 text-green-400")
            else:
                _autostart_lbl.text = "On, but the scheduler entry is missing — toggle off and on to repair."
                _autostart_lbl.classes(replace="text-xs mt-1 text-yellow-400")

        # Reverting the switch after a failure re-fires this handler, which
        # would run disable() a second time and stack a second notification on
        # top of the error the user actually needs to read.
        _autostart_reverting = {"active": False}

        def _revert_switch(to_value: bool):
            _autostart_reverting["active"] = True
            try:
                auto_restart_sw.value = to_value
            finally:
                _autostart_reverting["active"] = False
            _refresh_autostart_lbl()

        def _on_autostart_change(e):
            if _autostart_reverting["active"]:
                return
            want = bool(e.value)
            if want and not _autostart.is_supported():
                ui.notify(
                    f"Auto-restart is not supported on {sys.platform}",
                    type="warning", position="top",
                )
                _revert_switch(False)
                return
            try:
                if want:
                    _autostart.enable()
                else:
                    _autostart.disable()
            except Exception as exc:
                # Leave the stored setting alone when the OS refused — a toggle
                # showing ON with no scheduler entry behind it is exactly the
                # false sense of safety this feature exists to remove.
                ui.notify(f"Could not enable auto-restart: {exc}",
                          type="negative", position="top")
                _revert_switch(not want)
                return
            settings_ctl.set_app_config("auto_restart_enabled", "1" if want else "0")
            ui.notify(
                "Auto-restart enabled" if want else "Auto-restart disabled",
                type="positive" if want else "info", position="top",
            )
            _refresh_autostart_lbl()

        auto_restart_sw.on_value_change(_on_autostart_change)
        _refresh_autostart_lbl()

    with ui.row().classes("gap-2 mb-2 items-center flex-wrap"):
        ui.button("Run Diagnostics", on_click=run_diag).classes(
            "bg-blue-700 text-white px-4 py-2"
        )
        ui.button("Live Diagnostics", icon="terminal", on_click=_toggle_live_diag).classes(
            "bg-purple-700 text-white px-4 py-2"
        ).tooltip("Stream live app logs (errors, warnings and events) since last restart")
        ui.button("Export Logs", icon="mail", on_click=export_logs).classes(
            "bg-gray-700 text-white px-4 py-2"
        ).tooltip("Email the last 5 days of logs as a .txt attachment to the configured report address")
    export_lbl

    with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg mt-3"):
        with ui.row().classes("items-center gap-1 mb-2"):
            ui.label("Data Retention").classes("font-bold text-yellow-300")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Applies to this node's own database: Telegram messages, closed "
                "trades (open trades are never touched), and resolved signal/"
                "parse history (vantage_signals, vantage_tg_signals, unrecognised "
                "messages, AI-recovered signals). Checked once a day. "
                "Indefinite (default) never deletes anything — this matches how "
                "the app has always behaved; there was no retention limit before "
                "this setting existed."
            )
        ui.label(
            "How long historical data is kept before being automatically deleted."
        ).classes("text-xs text-gray-500 mb-3")

        _retention_options = {
            "0": "Indefinite (never delete)",
            "30": "30 days",
            "90": "90 days",
            "180": "180 days",
            "365": "365 days",
            "custom": "Custom…",
        }
        _current_days = settings_ctl.get_data_retention_days()
        _preset_days = {0, 30, 90, 180, 365}
        _initial_key = str(_current_days) if _current_days in _preset_days else "custom"

        with ui.row().classes("items-center gap-2 flex-wrap"):
            retention_select = ui.select(_retention_options, value=_initial_key).classes("w-56")
            custom_days_input = ui.number(
                value=_current_days if _initial_key == "custom" else 0,
                min=1, step=1, format="%.0f", placeholder="days",
            ).classes("w-24")
            custom_days_input.visible = _initial_key == "custom"
            save_retention_btn = ui.button("Save", icon="save").classes(
                "bg-blue-700 text-white"
            )

        def _on_retention_select_change(e):
            custom_days_input.visible = (e.value == "custom")

        retention_select.on_value_change(_on_retention_select_change)

        def _save_retention():
            key = retention_select.value
            if key == "custom":
                days = int(custom_days_input.value or 0)
                if days <= 0:
                    ui.notify(
                        "Enter a number of days greater than 0, or choose Indefinite.",
                        type="warning",
                    )
                    return
            else:
                days = int(key)
            settings_ctl.set_data_retention_days(days)
            ui.notify(
                "Data retention set to indefinite — nothing will be deleted."
                if days == 0 else
                f"Data retention set to {days} days — older Telegram messages, "
                f"closed trades, and signal history will be pruned daily.",
                type="positive",
            )

        save_retention_btn.on_click(_save_retention)

    live_diag_card
    diag_container

    return run_diag
