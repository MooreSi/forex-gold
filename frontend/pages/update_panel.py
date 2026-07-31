"""
Update panel — shown under Settings > Update for all users.

Displays the current app version, live connection status to the admin server,
the client registration/authorisation state, and the latest changelog.

The status cards refresh every 5 seconds so the page stays accurate without
the user having to navigate away and back.
"""

from pathlib import Path

from nicegui import ui

from backend.src.controllers.remote.client import (
    get_or_create_token, get_status, get_stored_email, request_registration, _app_version,
)
from backend.src.controllers.remote.tls import SERVER_HOST, SERVER_PORT


def _read_changelog() -> list[str]:
    cl = Path(__file__).resolve().parents[2] / "CHANGELOG.md"
    if cl.exists():
        return cl.read_text(encoding="utf-8").splitlines()
    return []


def render():
    version = _app_version()
    token   = get_or_create_token()

    with ui.column().classes("w-full gap-4"):

        # ── Update available banner (dynamic) ────────────────────────────────
        banner = ui.element("div").classes("w-full")

        # ── Admin server connection status (dynamic) ─────────────────────────
        conn_card = ui.element("div").classes("w-full")

        # ── Registration / authorisation status (dynamic) ────────────────────
        token_card = ui.element("div").classes("w-full")

        # ── Changelog (static) ───────────────────────────────────────────────
        cl_lines = _read_changelog()
        if cl_lines:
            with ui.card().classes(
                "w-full bg-gray-800 border border-gray-700 p-4 rounded-lg"
            ):
                with ui.row().classes("items-center gap-2 mb-2"):
                    ui.icon("list_alt", color="teal-400", size="xs")
                    ui.label("Changelog").classes("text-sm font-bold text-teal-300")
                with ui.scroll_area().style("max-height:300px"):
                    with ui.column().classes("gap-0"):
                        for line in cl_lines:
                            cls = (
                                "text-yellow-300 font-bold text-sm mt-2"
                                if line.startswith("## ")
                                else "text-gray-300 text-xs leading-relaxed"
                                if line.startswith("- ")
                                else "text-gray-500 text-xs"
                            )
                            display = line.lstrip("# -").strip() or "​"
                            ui.label(display).classes(cls)

        # ── Refresh all dynamic sections ─────────────────────────────────────
        def _refresh():
            status       = get_status()
            connected    = status.get("connected", False)
            update_av    = status.get("update_available", False)
            latest       = status.get("latest_version", "")
            last_err     = status.get("last_error", "")
            conn_host    = status.get("connected_host", "") or SERVER_HOST
            via_lan      = connected and conn_host != SERVER_HOST

            # ── Banner ───────────────────────────────────────────────────────
            banner.clear()
            if update_av and latest:
                with banner:
                    with ui.card().classes(
                        "w-full bg-green-900 border border-green-600 p-4 rounded-lg"
                    ):
                        with ui.row().classes("items-center gap-3"):
                            ui.icon("new_releases", color="green-300", size="md")
                            with ui.column().classes("gap-0 flex-1"):
                                ui.label(f"Update available: v{latest}").classes(
                                    "text-green-300 font-bold text-base"
                                )
                                ui.label(
                                    "Your administrator will push this update to you shortly. "
                                    "The app will restart automatically — you do not need to "
                                    "do anything."
                                ).classes("text-xs text-green-200 leading-relaxed mt-0.5")
                            ui.badge(
                                f"You have v{version}", color="grey"
                            ).classes("text-xs shrink-0")

            # ── Connection card ───────────────────────────────────────────────
            conn_card.clear()
            with conn_card:
                border = "border-green-700" if connected else "border-gray-700"
                with ui.card().classes(
                    f"w-full bg-gray-800 border {border} p-4 rounded-lg"
                ):
                    with ui.row().classes("items-center gap-3 mb-3"):
                        ui.icon("system_update", color="yellow-400")
                        ui.label("App Updates").classes(
                            "text-base font-bold text-yellow-300"
                        )

                    with ui.row().classes("gap-3 flex-wrap items-center"):
                        with ui.column().classes("gap-0"):
                            ui.label("Installed version").classes(
                                "text-xs text-gray-500 uppercase"
                            )
                            ui.label(f"v{version}").classes(
                                "text-xl font-bold font-mono text-yellow-300"
                            )

                        if latest and latest != version:
                            ui.element("div").classes("w-px bg-gray-700 self-stretch mx-1")
                            with ui.column().classes("gap-0"):
                                ui.label("Latest version").classes(
                                    "text-xs text-gray-500 uppercase"
                                )
                                ui.label(f"v{latest}").classes(
                                    "text-xl font-bold font-mono text-green-400"
                                )

                        ui.element("div").classes("w-px bg-gray-700 self-stretch mx-1")

                        with ui.column().classes("gap-1"):
                            ui.label("Admin server").classes(
                                "text-xs text-gray-500 uppercase"
                            )
                            ui.badge(
                                "Connected" if connected else "Offline / Not reachable",
                                color="green" if connected else "grey",
                            ).classes("text-xs")
                            if connected:
                                loc = "local network" if via_lan else SERVER_HOST
                                ui.label(loc).classes("text-xs text-gray-500 font-mono")
                            else:
                                ui.label(f"{SERVER_HOST}:{SERVER_PORT}").classes(
                                    "text-xs text-gray-600 font-mono"
                                )

                    if last_err and not connected:
                        ui.label(f"Last error: {last_err}").classes(
                            "text-xs text-orange-400 italic mt-1"
                        )

                    ui.label(
                        "Updates are pushed to you by the administrator. "
                        "When an update is available it downloads and applies automatically, "
                        "then the app restarts. You do not need to do anything."
                    ).classes("text-xs text-gray-400 leading-relaxed mt-2")

            # ── Token / registration card ─────────────────────────────────────
            token_card.clear()
            with token_card:
                if connected:
                    sub_type   = status.get("subscription_type", "") or "Perpetual"
                    sub_expiry = status.get("subscription_expiry", "") or "perpetual"
                    sub_email  = status.get("email", "")
                    if sub_expiry == "perpetual":
                        expiry_label = "Never"
                        expiry_cls   = "text-green-400"
                    else:
                        from datetime import date as _date
                        try:
                            exp   = _date.fromisoformat(sub_expiry)
                            delta = (exp - _date.today()).days
                            if delta < 0:
                                expiry_label = f"Expired ({sub_expiry})"
                                expiry_cls   = "text-red-400"
                            elif delta <= 30:
                                expiry_label = f"{sub_expiry} ({delta} days)"
                                expiry_cls   = "text-orange-400"
                            else:
                                expiry_label = f"{sub_expiry} ({delta} days)"
                                expiry_cls   = "text-green-400"
                        except Exception:
                            expiry_label = sub_expiry
                            expiry_cls   = "text-gray-400"

                    with ui.card().classes(
                        "w-full bg-gray-800 border border-green-800 p-4 rounded-lg"
                    ):
                        with ui.row().classes("items-center gap-2 mb-3"):
                            ui.icon("verified", color="green-400", size="xs")
                            ui.label("Authorised and Registered").classes(
                                "text-sm font-bold text-green-300"
                            )
                        with ui.grid(columns=2).classes("gap-x-6 gap-y-1 text-xs mb-3"):
                            ui.label("Subscription").classes("text-gray-500")
                            ui.label(sub_type).classes("text-white font-semibold")
                            ui.label("Expires").classes("text-gray-500")
                            ui.label(expiry_label).classes(expiry_cls + " font-mono")
                            if sub_email:
                                ui.label("Email").classes("text-gray-500")
                                ui.label(sub_email).classes("text-gray-300 font-mono")
                        with ui.row().classes(
                            "items-center gap-2 bg-gray-900 rounded p-2"
                        ):
                            ui.label(token).classes(
                                "text-xs font-mono text-gray-600 flex-1 break-all"
                            )
                            ui.button(
                                icon="content_copy",
                                on_click=lambda: (
                                    ui.clipboard.write(token),
                                    ui.notify("Token copied", type="positive"),
                                ),
                            ).classes("bg-gray-700 text-white text-xs").tooltip(
                                "Copy token"
                            )
                elif last_err == "awaiting admin approval":
                    with ui.card().classes(
                        "w-full bg-gray-800 border border-yellow-800 p-4 rounded-lg"
                    ):
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.icon("pending", color="yellow-400", size="xs")
                            ui.label("Awaiting Administrator Approval").classes(
                                "text-sm font-bold text-yellow-300"
                            )
                        ui.label(
                            "Your registration request has been sent. The administrator "
                            "will review and approve your machine — this page updates "
                            "automatically."
                        ).classes("text-xs text-gray-400 leading-relaxed")
                else:
                    stored_email = get_stored_email()
                    with ui.card().classes(
                        "w-full bg-gray-800 border border-blue-900 p-4 rounded-lg"
                    ):
                        with ui.row().classes("items-center gap-2 mb-2"):
                            ui.icon("how_to_reg", color="blue-400", size="xs")
                            ui.label("Request Registration").classes(
                                "text-sm font-bold text-blue-300"
                            )
                        ui.label(
                            "Enter your email address and click the button below to send a "
                            "registration request to the administrator."
                        ).classes("text-xs text-gray-400 leading-relaxed mb-3")
                        email_inp = ui.input(
                            "Email address",
                            value=stored_email,
                            placeholder="you@example.com",
                        ).classes("w-full mb-2").props('autocomplete="off" type="email"')

                        def _send_reg():
                            e = email_inp.value.strip()
                            if not e or "@" not in e:
                                ui.notify("Enter a valid email address", type="warning")
                                return
                            request_registration(e)
                            ui.notify("Registration request sent", type="positive")

                        ui.button(
                            "Request Registration",
                            icon="send",
                            on_click=_send_reg,
                        ).classes("bg-blue-800 text-white text-sm px-4 py-2")
                        ui.separator().classes("my-3")
                        ui.label("Your machine token (unique to this installation):").classes(
                            "text-xs text-gray-500 mb-1"
                        )
                        with ui.row().classes(
                            "items-center gap-2 bg-gray-900 rounded p-2"
                        ):
                            ui.label(token).classes(
                                "text-xs font-mono text-gray-400 flex-1 break-all"
                            )
                            ui.button(
                                icon="content_copy",
                                on_click=lambda: (
                                    ui.clipboard.write(token),
                                    ui.notify("Token copied", type="positive"),
                                ),
                            ).classes("bg-gray-700 text-white text-xs").tooltip(
                                "Copy token"
                            )

        _refresh()
        ui.timer(5, _refresh)
