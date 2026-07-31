"""
Settings > Remote Node — configure this machine's role in the Local/Remote
sync feature: either the VPS (accept connections) or the Mac (connect out
to a VPS), never both. See forex_trader/sync/ for the protocol.
"""
from __future__ import annotations

import asyncio
import logging

from nicegui import ui

from backend.src.db import database as db_module
from backend.src.controllers.sync import tls_util

log = logging.getLogger(__name__)

_DEFAULT_PORT = tls_util.DEFAULT_SYNC_PORT


def _get_sub_engines():
    from backend.src.services.breakout_signal import breakout_signal_service as _bo_mod
    from backend.src.services.test_signal import test_signal_service as _bc_mod
    from backend.src.services.reversal_engine import reversal_engine_service as _gd_mod
    return _bo_mod.get_instance(), _bc_mod.get_instance(), _gd_mod.get_instance()


def render(get_engine=None) -> None:
    with ui.column().classes("w-full gap-4 max-w-2xl"):
        ui.label(
            "Configure exactly one role per machine: the VPS accepts the "
            "connection, the Mac initiates it. Both roles use the same "
            "shared token — generate it on the VPS, paste it here on the Mac."
        ).classes("text-xs text-gray-500")

        # ── Role: this machine is the VPS (server) ──────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
            ui.label("This machine is the VPS").classes("text-base font-bold text-yellow-300")
            server_enabled = ui.switch(
                "Accept remote connections",
                value=bool(int(db_module.get_app_config("sync_server_enabled") or "0")),
            )
            port_input = ui.number(
                "Listen port", value=int(db_module.get_app_config("sync_server_port") or _DEFAULT_PORT),
                min=1024, max=65535,
            ).classes("w-40")
            fingerprint_lbl = ui.label("").classes("text-xs text-gray-400 font-mono break-all")
            token_lbl = ui.label("").classes("text-xs text-yellow-400 font-mono break-all mt-1")
            server_status_lbl = ui.label("stopped").classes("text-xs text-gray-500")

            def _refresh_fingerprint():
                fp = tls_util.cert_fingerprint()
                fingerprint_lbl.text = f"Cert fingerprint: {fp}" if fp else \
                    "Cert fingerprint: (generated on first start)"

            async def _generate_token():
                tok = db_module.generate_sync_token()
                token_lbl.text = f"Token (copy to the Mac's settings — shown once): {tok}"
                ui.notify("New token generated. Any previously-paired Mac must be reconfigured.",
                           type="warning")

            async def _toggle_server(e):
                from backend.src.controllers.sync import server as sync_server
                db_module.set_app_config("sync_server_enabled", "1" if e.value else "0")
                db_module.set_app_config("sync_server_port", str(int(port_input.value)))
                if e.value:
                    token = db_module.get_sync_token()
                    if not token:
                        ui.notify("Generate a token first.", type="negative")
                        server_enabled.value = False
                        return
                    try:
                        import socket
                        host = socket.gethostbyname(socket.gethostname())
                    except Exception:
                        host = "0.0.0.0"
                    bo, bc, gd = _get_sub_engines()
                    main_eng = get_engine() if get_engine else None
                    srv = sync_server.init(
                        main_engine=main_eng, breakout_engine=bo,
                        bounce_engine=bc, re_engine=gd,
                    )
                    try:
                        await srv.start(host, int(port_input.value), token)
                        server_status_lbl.text = f"listening on port {int(port_input.value)}"
                        _refresh_fingerprint()
                        ui.notify("Sync server started.", type="positive")
                    except Exception as exc:
                        server_status_lbl.text = f"failed to start: {exc}"
                        ui.notify(f"Failed to start sync server: {exc}", type="negative")
                        server_enabled.value = False
                else:
                    srv = sync_server.get_instance()
                    if srv is not None:
                        await srv.stop()
                    server_status_lbl.text = "stopped"
                    ui.notify("Sync server stopped.", type="warning")

            with ui.row().classes("gap-2 mt-2"):
                ui.button("Generate new token", on_click=_generate_token).props("outline dense")
            server_enabled.on_value_change(_toggle_server)
            _refresh_fingerprint()

        # ── Headless mode (no web UI) ────────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
            ui.label("Headless mode").classes("text-base font-bold text-yellow-300")
            ui.label(
                "Runs the trading engine, Telegram reader, MT5 bridge, and sync "
                "without the web UI at all — no browser tab overhead. Typically "
                "used on the VPS once you're relying on the Mac's dashboard for "
                "VPS-origin trade detail. Requires a restart to take effect "
                "(this switch triggers one immediately)."
            ).classes("text-xs text-gray-500")
            headless_enabled = ui.switch(
                "Run headless (no web UI)",
                value=bool(int(db_module.get_app_config("headless_mode_enabled") or "0")),
            )
            headless_status_lbl = ui.label("").classes("text-xs text-gray-500")

            async def _toggle_headless(e):
                db_module.set_app_config("headless_mode_enabled", "1" if e.value else "0")
                engine = get_engine() if get_engine else None
                if engine is None:
                    ui.notify("Setting saved — restart the app for it to take effect.",
                              type="warning")
                    return
                headless_status_lbl.text = "Restarting to apply…"
                result = await engine._cmd_restart_app([])
                ui.notify(result, type="info")

            headless_enabled.on_value_change(_toggle_headless)

        # ── Role: this machine connects to a VPS (client) ───────────────────
        with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
            ui.label("Connect to a remote VPS").classes("text-base font-bold text-yellow-300")
            _host0, _port0, _tok0 = _load_client_config()
            host_input  = ui.input("VPS IP or hostname", value=_host0).classes("w-full")
            cport_input = ui.number("Port", value=_port0, min=1024, max=65535).classes("w-40")
            token_input = ui.input("Shared token (from the VPS)", value=_tok0,
                                    password=True, password_toggle_button=True).classes("w-full")
            conn_status_lbl = ui.label("disconnected").classes("text-xs text-gray-500")
            remote_status_box = ui.column().classes("w-full gap-1 mt-2")

            async def _save_and_connect():
                from backend.src.controllers.sync import client as sync_client
                cli = sync_client.get_instance()
                host, port, token = host_input.value.strip(), int(cport_input.value), token_input.value.strip()
                if not host or not token:
                    ui.notify("Host and token are required.", type="negative")
                    return
                cli.configure(host, port, token)
                cli.start(host, port, token)
                ui.notify("Connecting…", type="info")

            async def _disconnect():
                from backend.src.controllers.sync import client as sync_client
                sync_client.get_instance().stop()
                ui.notify("Disconnected from VPS.", type="warning")

            with ui.row().classes("gap-2 mt-2"):
                ui.button("Save & Connect", on_click=_save_and_connect).props("color=primary")
                ui.button("Disconnect", on_click=_disconnect).props("outline")

            def _tick_status():
                from backend.src.controllers.sync import client as sync_client
                cli = sync_client.get_instance()
                conn_status_lbl.text = f"status: {cli.conn_state}" + (
                    f" ({cli.last_error})" if cli.last_error and cli.conn_state != "connected" else ""
                )
                remote_status_box.clear()
                if cli.conn_state == "connected" and cli.remote_status:
                    s = cli.remote_status
                    with remote_status_box:
                        ui.label(
                            f"VPS balance: ${s.get('balance') or 0:,.2f}  "
                            f"equity: ${s.get('equity') or 0:,.2f}"
                        ).classes("text-xs text-gray-300")
                        ui.label(
                            f"Open positions: {len(s.get('open_positions') or [])}  "
                            f"Active trader: {s.get('active_trader', '?')}"
                        ).classes("text-xs text-gray-300")
                        engines = s.get("engines") or {}
                        ui.label(
                            "Engines: " + ", ".join(f"{k}={'on' if v else 'off'}" for k, v in engines.items())
                        ).classes("text-xs text-gray-400")
                        if s.get("cpu_percent") is not None:
                            cpu = s.get("cpu_percent") or 0.0
                            mem_used = s.get("mem_used_mb") or 0.0
                            mem_total = s.get("mem_total_mb") or 0.0
                            mem_pct = s.get("mem_percent") or 0.0
                            cpu_col = "#f87171" if cpu >= 85 else ("#fbbf24" if cpu >= 60 else "#4ade80")
                            mem_col = "#f87171" if mem_pct >= 85 else ("#fbbf24" if mem_pct >= 60 else "#4ade80")
                            with ui.row().classes("gap-4 mt-1"):
                                ui.label(f"CPU: {cpu:.0f}%").classes("text-xs font-bold").style(f"color:{cpu_col}")
                                ui.label(
                                    f"Memory: {mem_used:,.0f} / {mem_total:,.0f} MB ({mem_pct:.0f}%)"
                                ).classes("text-xs font-bold").style(f"color:{mem_col}")

            ui.timer(2.0, _tick_status)

        # ── Centralized signal generation ────────────────────────────────────
        with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
            ui.label("Generate signals on this node only").classes(
                "text-base font-bold text-yellow-300"
            )
            ui.label(
                "When on and the VPS is the active trader (Remote mode): the "
                "VPS stops running Breakout/Bounce/Reversal Engine analysis and GD2/"
                "GD VIP Telegram parsing entirely, and only executes trades "
                "forwarded to it from this Mac's own generators. Reduces load "
                "on the VPS, but this Mac becomes the only source of new "
                "trades — if it goes offline, the VPS does NOT fall back to "
                "generating its own signals; it alerts via Telegram/email and "
                "waits. Local mode (Mac as active trader) is unaffected either "
                "way — this Mac always generates and executes locally there."
            ).classes("text-xs text-gray-500")
            centralized_enabled = ui.switch(
                "Generate signals on this node only, forward to the active trader",
                value=bool(db_module.get_risk_settings().get("centralized_signal_gen_enabled")),
            )

            def _toggle_centralized(e):
                db_module.update_risk_settings({"centralized_signal_gen_enabled": 1 if e.value else 0})
                ui.notify(
                    "Centralized signal generation " + ("enabled." if e.value else "disabled."),
                    type="warning" if e.value else "info",
                )

            centralized_enabled.on_value_change(_toggle_centralized)

        # ── Model snapshot transfer (manual, either direction) ──────────────
        with ui.card().classes("w-full bg-gray-800 p-4 rounded-lg"):
            ui.label("ML model snapshot").classes("text-base font-bold text-yellow-300")
            ui.label(
                "One-off copy of the trained model files — does not run "
                "automatically. Use this to seed a freshly set-up node with "
                "the other side's more mature models."
            ).classes("text-xs text-gray-500")
            transfer_status_lbl = ui.label("").classes("text-xs text-gray-400 mt-1")

            async def _download_models():
                from backend.src.controllers.sync import client as sync_client
                cli = sync_client.get_instance()
                if cli.conn_state != "connected":
                    ui.notify("Not connected to a VPS.", type="negative")
                    return
                transfer_status_lbl.text = "downloading…"
                try:
                    await cli.request_model_snapshot("download")
                    transfer_status_lbl.text = "download complete"
                    ui.notify("Model snapshot downloaded from VPS.", type="positive")
                except Exception as exc:
                    transfer_status_lbl.text = f"failed: {exc}"
                    ui.notify(f"Download failed: {exc}", type="negative")

            async def _upload_models():
                from backend.src.controllers.sync import client as sync_client
                cli = sync_client.get_instance()
                if cli.conn_state != "connected":
                    ui.notify("Not connected to a VPS.", type="negative")
                    return
                transfer_status_lbl.text = "uploading…"
                try:
                    await cli.request_model_snapshot("upload")
                    transfer_status_lbl.text = "upload complete"
                    ui.notify("Model snapshot uploaded to VPS.", type="positive")
                except Exception as exc:
                    transfer_status_lbl.text = f"failed: {exc}"
                    ui.notify(f"Upload failed: {exc}", type="negative")

            with ui.row().classes("gap-2"):
                ui.button("Download from VPS", on_click=_download_models).props("outline dense")
                ui.button("Upload to VPS", on_click=_upload_models).props("outline dense")


def _load_client_config() -> tuple[str, int, str]:
    from backend.src.controllers.sync.client import SyncClient
    return SyncClient.load_config()
