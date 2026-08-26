"""Email tab: SMTP credentials, the alert toggles and the test-send flow."""
from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl


def _smtp_friendly_error(raw: str) -> str:
    """Translate raw SMTP exception strings into actionable user messages."""
    r = raw.lower()

    # Outlook/Microsoft 535 — basic auth disabled (Authenticated SMTP toggle not on)
    if "535" in r and ("basic authentication" in r or "authentication unsuccessful" in r or "5.7.139" in r):
        return (
            "✗  Microsoft rejected the login: SMTP AUTH (basic authentication) is disabled.\n\n"
            "FIX — enable Authenticated SMTP in your Outlook account:\n"
            "  1. Open outlook.live.com and sign in\n"
            "  2. Click Settings gear → View all Outlook settings\n"
            "  3. Go to Mail → Sync email\n"
            "  4. Toggle 'Authenticated SMTP' to ON and click Save\n"
            "  5. Come back here and click Send Test Email again\n\n"
            "This is a Microsoft account setting, not an app issue.\n"
            f"Original error: {raw[:120]}"
        )

    # Gmail 535 — wrong app password or 2FA not enabled
    if "535" in r and "gmail" in r:
        return (
            "✗  Gmail rejected the login.\n"
            "Make sure you are using an App Password (not your regular password) "
            "and that 2-Step Verification is enabled on your Google account.\n"
            f"Error: {raw[:120]}"
        )

    # Generic 535 authentication
    if "535" in r:
        return (
            "✗  Authentication failed (535) — wrong username or password.\n"
            "If you have 2FA / two-step verification, you MUST use an App Password, "
            "not your regular account password.\n"
            f"Error: {raw[:120]}"
        )

    # Connection refused / timeout
    if "connection refused" in r or "timed out" in r or "network" in r:
        return (
            "✗  Could not connect to the mail server.\n"
            "Check the SMTP host and port are correct, and that your firewall "
            "allows outbound connections on port 587.\n"
            f"Error: {raw[:120]}"
        )

    # Certificate error
    if "certificate" in r or "ssl" in r:
        return (
            "✗  SSL/TLS certificate error.\n"
            "Try enabling 'Use TLS / STARTTLS encryption' if it is off, "
            "or switch to port 587 (STARTTLS) instead of 465.\n"
            f"Error: {raw[:120]}"
        )

    # Fallback
    return f"✗  Failed: {raw}"


def _render_email():
    ecfg = settings_ctl.get_email_config()

    # ── Schedule & Delivery card (top) ────────────────────────────────────────
    _SEND_PROVIDERS = {
        "resend":  "Resend (API — recommended)",
        "gmail":   "Gmail (SMTP)",
        "outlook": "Outlook.com (SMTP)",
        "custom":  "Custom SMTP",
    }

    def _default_send_provider() -> str:
        stored = ecfg.get("send_provider") or ""
        if stored in _SEND_PROVIDERS:
            return stored
        # Auto-detect from what is configured
        if (ecfg.get("resend_api_key") or "").strip():
            return "resend"
        h = (ecfg.get("smtp_host") or "").lower()
        if "gmail" in h:
            return "gmail"
        if "outlook" in h or "office365" in h:
            return "outlook"
        return "resend"

    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg mb-4"):
        ui.label("Schedule & Delivery").classes("text-lg font-bold text-yellow-400 mb-1")
        ui.label(
            "Choose which provider sends the reports, then set the schedule."
        ).classes("text-sm text-gray-400 mb-4")

        ui.label("Send reports via").classes("text-sm font-semibold text-gray-300 mb-1")
        with ui.row().classes("w-full items-center gap-1"):
            send_provider_sel = ui.select(
                _SEND_PROVIDERS,
                value=_default_send_provider(),
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Select which configured provider is used for scheduled daily and weekly reports. "
                "Configure the provider's credentials in the section below, then come back here to save."
            )

        ui.separator().classes("my-4")

        ui.label("Reports").classes("text-sm font-semibold text-gray-300 mb-2")
        with ui.row().classes("gap-6 flex-wrap"):
            with ui.row().classes("items-center gap-1"):
                daily_enabled = ui.checkbox(
                    "Daily summary",
                    value=bool(ecfg.get("daily_enabled", 0)),
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Sends a performance email every day at the time below."
                )

            with ui.row().classes("items-center gap-1"):
                weekly_enabled = ui.checkbox(
                    "Weekly summary (Fridays only)",
                    value=bool(ecfg.get("weekly_enabled", 0)),
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Sends a weekly summary every Friday. On Fridays you receive both "
                    "a daily and a weekly email as separate messages."
                )

            with ui.row().classes("items-center gap-1"):
                orb_enabled = ui.checkbox(
                    "Morning ORB / IVB report (08:15 UK time)",
                    value=bool(ecfg.get("orb_report_enabled", 1)),
                )
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Sends the Asian session range (00:00-08:00 UTC) with a "
                    "volume-profile overlay (POC/value area), reload-zone entry, stop, "
                    "and target — plus a chart image — every weekday at 08:15 "
                    "Europe/London, shortly after London opens and the breakout has a "
                    "moment to establish direction (adjusts for BST/GMT automatically), "
                    "independent of the send time below."
                )

        with ui.row().classes("items-center gap-1 mt-2"):
            send_time = ui.input(
                "Send time (24-hour, HH:MM)",
                value=ecfg.get("send_time", "18:00") or "18:00",
            ).classes("w-40")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Time of day to send reports. Uses the server's local clock. "
                "18:00 = 6 PM.  The app checks the time every minute."
            )

        def save_schedule():
            settings_ctl.save_email_config({
                "send_provider":      send_provider_sel.value or "resend",
                "daily_enabled":      int(daily_enabled.value),
                "weekly_enabled":     int(weekly_enabled.value),
                "send_time":          send_time.value or "18:00",
                "orb_report_enabled": int(orb_enabled.value),
            })
            ui.notify("Schedule saved", type="positive")

        schedule_test_lbl = ui.label("").classes("text-sm mt-3")

        async def test_scheduled_email():
            from backend.src.services.notifications import email_service
            provider = send_provider_sel.value or "resend"
            fresh = settings_ctl.get_email_config()
            to_addr = (fresh.get("to_addr") or "").strip()
            if not to_addr:
                schedule_test_lbl.text = "Set 'Send reports to' in the SMTP section below first"
                schedule_test_lbl.classes(replace="text-sm text-orange-400")
                return
            # Pass full DB config but force selected provider so routing is correct
            fresh["send_provider"] = provider
            html = """<html><body style="background:#111827;padding:24px;font-family:Arial;">
            <p style="color:#f59e0b;font-size:22px;font-weight:bold;">FOREX Trader</p>
            <p style="color:#e5e7eb;">Delivery test — scheduled reports will be sent via this provider.</p>
            </body></html>"""
            schedule_test_lbl.text = f"Sending via {provider}..."
            schedule_test_lbl.classes(replace="text-sm text-gray-400")
            ok, err = await email_service.send_email("FOREX Trader — Delivery Test", html, fresh)
            if ok:
                schedule_test_lbl.text = f"Sent via {provider} to {to_addr}"
                schedule_test_lbl.classes(replace="text-sm text-green-400 font-semibold")
                ui.notify(f"Test sent via {provider}", type="positive")
            else:
                schedule_test_lbl.text = f"Failed: {err}"
                schedule_test_lbl.classes(replace="text-sm text-red-300")

        async def test_orb_email():
            from datetime import datetime as _dt
            from backend.src.services.notifications import email_service
            fresh = settings_ctl.get_email_config()
            to_addr = (fresh.get("to_addr") or "").strip()
            if not to_addr:
                schedule_test_lbl.text = "Set 'Send reports to' in the SMTP section below first"
                schedule_test_lbl.classes(replace="text-sm text-orange-400")
                return
            schedule_test_lbl.text = "Building ORB report..."
            schedule_test_lbl.classes(replace="text-sm text-gray-400")
            from backend.src.app import get_engine as _get_engine
            engine = _get_engine()
            report = await engine.build_orb_report()
            if not report:
                schedule_test_lbl.text = "Could not build report — MT5 bridge/candles not available right now"
                schedule_test_lbl.classes(replace="text-sm text-red-300")
                return
            chart_png = email_service.build_orb_chart_image(report)
            html = email_service.build_orb_html(
                report, _dt.now().strftime("%A, %d %B %Y"),
                has_chart=bool(chart_png),
            )
            ok, err = await email_service.send_email(
                "FOREX Trader — London Open ORB Report (test)", html, fresh,
                image_bytes=chart_png, image_cid=email_service._ORB_CHART_CID,
            )
            if ok:
                schedule_test_lbl.text = f"ORB test sent to {to_addr}"
                schedule_test_lbl.classes(replace="text-sm text-green-400 font-semibold")
                ui.notify("ORB test email sent", type="positive")
            else:
                schedule_test_lbl.text = f"Failed: {err}"
                schedule_test_lbl.classes(replace="text-sm text-red-300")

        with ui.row().classes("gap-3 mt-4"):
            ui.button("Save Schedule", icon="save", on_click=save_schedule).classes(
                "bg-blue-700 text-white px-5 py-2"
            )
            ui.button("Test Delivery", icon="send", on_click=test_scheduled_email).classes(
                "bg-gray-600 text-white px-5 py-2"
            )
            ui.button("Send Test ORB Report", icon="send", on_click=test_orb_email).classes(
                "bg-gray-600 text-white px-5 py-2"
            )
        schedule_test_lbl

    # ── Resend card ───────────────────────────────────────────────────────────
    with ui.card().classes("w-full max-w-2xl rounded-lg p-0 overflow-hidden mb-4").style(
        "border:2px solid #6366f1;"
    ):
        with ui.row().classes("w-full px-4 py-2 items-center gap-2").style(
            "background:#0e0e2a;"
        ):
            ui.label("Resend API").classes("text-sm font-bold text-indigo-300")
            ui.badge("NO SMTP NEEDED", color="indigo").classes("text-xs")
            ui.label("3,000 emails/month free · no domain required").classes(
                "text-xs text-indigo-400 ml-auto"
            )

        with ui.column().classes("px-5 py-4 gap-3 bg-gray-800 w-full"):
            ui.label(
                "Resend uses a secure HTTPS API — no app passwords, no SMTP ports. "
                "Works immediately with your email as the recipient. "
                "Get an API key in under 2 minutes at resend.com (free, no credit card)."
            ).classes("text-xs text-gray-300 leading-relaxed")

            with ui.expansion("Setup steps (2 min)", icon="help_outline").classes(
                "w-full bg-gray-700 rounded text-xs"
            ):
                with ui.column().classes("p-3 gap-1"):
                    for step in [
                        "1.  Go to  resend.com  and click  Get started for free",
                        "2.  Sign up with your email — no credit card required",
                        "3.  Go to  API Keys  in the left sidebar",
                        "4.  Click  Create API Key  — give it any name (e.g. FOREX Trader)",
                        "5.  Copy the key (starts with re_) and paste it below",
                        "6.  Set your own email as the 'Send reports to' address in the SMTP section",
                        "Note: emails arrive from  onboarding@resend.dev unless you verify",
                        "      your own domain at  resend.com/domains  (optional).",
                    ]:
                        ui.label(step).classes("text-xs text-gray-200 font-mono leading-relaxed")

            with ui.row().classes("w-full items-center gap-1"):
                rs_key = ui.input(
                    "Resend API Key  (starts with re_...)",
                    value=ecfg.get("resend_api_key", "") or "",
                    password=True,
                ).classes("flex-1")
                ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                    "Your Resend API key — find it at resend.com → API Keys"
                )

            rs_result = ui.label("").classes("text-sm")

            async def test_resend():
                rs_result.text = "Testing Resend..."
                rs_result.classes(replace="text-sm text-gray-400")
                from backend.src.services.notifications import email_service
                _fresh = settings_ctl.get_email_config()
                cfg_snap = {
                    "resend_api_key": rs_key.value,
                    "to_addr": _fresh.get("to_addr") or _fresh.get("smtp_user") or "",
                    "from_addr": _fresh.get("from_addr") or "",
                }
                if not cfg_snap["to_addr"]:
                    rs_result.text = "Set your 'Send reports to' address in the SMTP section below first"
                    rs_result.classes(replace="text-sm text-orange-400")
                    return
                html = """<html><body style="background:#111827;padding:24px;font-family:Arial;">
                <p style="color:#6366f1;font-size:22px;font-weight:bold;">FOREX Trader</p>
                <p style="color:#e5e7eb;">Resend connection test successful.</p>
                </body></html>"""
                ok, err = await email_service.send_email(
                    "FOREX Trader — Resend Test", html, cfg_snap
                )
                if ok:
                    rs_result.text = f"Sent via Resend to {cfg_snap['to_addr']}"
                    rs_result.classes(replace="text-sm text-green-400 font-semibold")
                    ui.notify("Resend test sent!", type="positive")
                else:
                    rs_result.text = f"Failed: {err}"
                    rs_result.classes(replace="text-sm text-red-300")

            def save_resend():
                settings_ctl.save_email_config({"resend_api_key": rs_key.value or ""})
                ui.notify("Resend API key saved", type="positive")

            with ui.row().classes("gap-3 mt-1"):
                ui.button("Save Key", icon="save", on_click=save_resend).classes(
                    "bg-indigo-700 text-white px-4 py-2"
                )
                ui.button("Test Resend", icon="send", on_click=test_resend).classes(
                    "bg-gray-600 text-white px-4 py-2"
                )
            rs_result

    ui.label("— or use SMTP directly —").classes(
        "text-xs text-gray-500 text-center w-full max-w-2xl my-1"
    )

    # ── Provider presets ──────────────────────────────────────────────────────
    _PROVIDERS = {
        "gmail":   {
            "label":    "Gmail",
            "host":     "smtp.gmail.com",
            "port":     587,
            "tls":      True,
            "pw_label": "App Password",
            "username_hint": "your.address@gmail.com",
        },
        "outlook": {
            "label":    "Outlook.com (personal @outlook / @hotmail)",
            "host":     "smtp-mail.outlook.com",
            "port":     587,
            "tls":      True,
            "pw_label": "App Password",
            "username_hint": "your.address@outlook.com",
        },
        "custom":  {
            "label":    "Custom / Other",
            "host":     "",
            "port":     587,
            "tls":      True,
            "pw_label": "Password",
            "username_hint": "your@email.com",
        },
    }

    def _detect_provider() -> str:
        h = (ecfg.get("smtp_host") or "").lower()
        if "gmail" in h:   return "gmail"
        if "office365" in h or "outlook.com" in h: return "outlook"
        return "custom"

    _sel_provider = [_detect_provider()]

    # ── SMTP card ─────────────────────────────────────────────────────────────
    with ui.card().classes("w-full max-w-2xl bg-gray-800 p-6 rounded-lg"):
        ui.label("SMTP Settings").classes("text-lg font-bold text-yellow-400 mb-1")
        ui.label(
            "Configure Gmail or Outlook SMTP credentials. "
            "Address settings here are also used by Resend as the recipient."
        ).classes("text-sm text-gray-400 mb-4")

        # ── Provider selector ─────────────────────────────────────────────────
        ui.label("Email Provider").classes("text-sm font-semibold text-gray-300 mb-1")
        with ui.row().classes("w-full items-center gap-1"):
            provider_sel = ui.select(
                {k: v["label"] for k, v in _PROVIDERS.items()},
                value=_sel_provider[0],
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Select your email provider. Settings are filled in automatically. "
                "Choose Custom to enter your own SMTP server details."
            )

        ui.separator().classes("my-3")

        # ── How-to card (shown for Gmail and Outlook) ─────────────────────────
        howto_card = ui.card().classes("w-full rounded-lg p-4 mb-1").style(
            "background:#1c1a0e; border:1px solid #d97706;"
        )

        def _build_howto(provider: str):
            howto_card.clear()
            with howto_card:
                if provider == "gmail":
                    ui.label("How to get a Gmail App Password").classes(
                        "text-sm font-bold text-yellow-300 mb-2"
                    )
                    steps = [
                        "1.  Go to  myaccount.google.com",
                        "2.  Click  Security  in the left sidebar",
                        "3.  Under 'How you sign in to Google', open  2-Step Verification",
                        "     (enable it first if not already on)",
                        "4.  Scroll to the bottom of that page and click  App passwords",
                        "5.  Choose app: Mail  →  device: Other → type 'FOREX Trader'",
                        "6.  Click  Generate — copy the 16-character password shown",
                        "7.  Paste it into the App Password field below",
                    ]
                    for s in steps:
                        ui.label(s).classes(
                            "text-xs text-gray-200 font-mono leading-relaxed"
                        )
                    ui.label(
                        "Note: the App Password is shown only once. "
                        "You can revoke and regenerate it at any time."
                    ).classes("text-xs text-gray-400 mt-2 italic")

                elif provider == "outlook":
                    ui.label("Outlook.com personal — Two mandatory steps").classes(
                        "text-sm font-bold text-yellow-300 mb-2"
                    )

                    with ui.row().classes("w-full items-start gap-2 p-3 rounded mb-3").style(
                        "background:#3b0a0a; border:2px solid #ef4444;"
                    ):
                        ui.label("warning").classes("material-icons text-red-400 text-base shrink-0 mt-0.5")
                        with ui.column().classes("gap-0.5"):
                            ui.label(
                                "Error 535 'basic authentication is disabled'?"
                            ).classes("text-xs font-bold text-red-300")
                            ui.label(
                                "Microsoft disables SMTP AUTH by default. You MUST enable the "
                                "'Authenticated SMTP' toggle in Outlook settings (Step 1 below) "
                                "— this is separate from TLS encryption and from the App Password. "
                                "Without it, even a correct App Password is rejected at the server."
                            ).classes("text-xs text-red-200 leading-relaxed")

                    with ui.row().classes("items-start gap-2 mb-3"):
                        ui.label("1").classes(
                            "text-xs font-bold bg-red-600 text-white rounded-full "
                            "w-5 h-5 flex items-center justify-center shrink-0 mt-0.5"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Enable Authenticated SMTP  (fixes error 535)").classes(
                                "text-xs font-semibold text-red-300"
                            )
                            for s in [
                                "a.  Open  outlook.live.com  in a browser and sign in",
                                "b.  Click the Settings gear  →  View all Outlook settings",
                                "c.  Go to  Mail  →  Sync email",
                                "d.  Scroll to the  'POP and IMAP'  section",
                                "e.  Toggle  Authenticated SMTP  to ON (blue)",
                                "f.  Click  Save  — it takes effect immediately",
                            ]:
                                ui.label(s).classes("text-xs text-gray-200 font-mono")
                            ui.label(
                                "Direct link: outlook.live.com → Settings → Mail → Sync email"
                            ).classes("text-xs text-blue-400 mt-1 italic")

                    with ui.row().classes("items-start gap-2"):
                        ui.label("2").classes(
                            "text-xs font-bold bg-yellow-600 text-black rounded-full "
                            "w-5 h-5 flex items-center justify-center shrink-0 mt-0.5"
                        )
                        with ui.column().classes("gap-1"):
                            ui.label("Create an App Password  (if 2FA is enabled on your account)").classes(
                                "text-xs font-semibold text-white"
                            )
                            for s in [
                                "a.  Go to  account.microsoft.com",
                                "b.  Click  Security  in the top nav",
                                "c.  Under 'Advanced security options' click  Get started",
                                "d.  Scroll to  App passwords  →  Create a new app password",
                                "e.  Copy the password and paste it in the App Password field below",
                            ]:
                                ui.label(s).classes("text-xs text-gray-200 font-mono")
                            ui.label(
                                "App Password is shown only once — save it. "
                                "Without 2FA you can use your normal account password."
                            ).classes("text-xs text-gray-400 mt-1 italic")

                else:
                    howto_card.style("display:none")

        _build_howto(_sel_provider[0])
        if _sel_provider[0] == "custom":
            howto_card.style("display:none")

        # ── SMTP fields ───────────────────────────────────────────────────────
        ui.label("Connection").classes("text-sm font-semibold text-gray-300 mb-1 mt-3")

        with ui.row().classes("w-full items-center gap-2"):
            smtp_host = ui.input(
                "SMTP Host",
                value=ecfg.get("smtp_host", "") or _PROVIDERS[_sel_provider[0]]["host"],
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The outgoing mail server address for your provider."
            )

            smtp_port = ui.number(
                "Port",
                value=int(ecfg.get("smtp_port") or _PROVIDERS[_sel_provider[0]]["port"]),
                min=1, max=65535, step=1, format="%.0f",
            ).classes("w-24")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "587 = STARTTLS (recommended).  465 = SSL.  25 = plain (avoid)."
            )

        with ui.row().classes("items-center gap-1"):
            use_tls = ui.checkbox(
                "Use TLS / STARTTLS encryption (strongly recommended)",
                value=bool(ecfg.get("use_tls", 1)),
            )
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Must be on for Gmail and Outlook. Encrypts the connection so your "
                "password is not sent in plain text."
            )

        ui.separator().classes("my-3")
        ui.label("Account").classes("text-sm font-semibold text-gray-300 mb-1")

        with ui.row().classes("w-full items-center gap-1"):
            smtp_user = ui.input(
                "Email address (username)",
                value=ecfg.get("smtp_user", "") or "",
                placeholder=_PROVIDERS[_sel_provider[0]]["username_hint"],
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Your full email address — this is used as the login username."
            )

        with ui.row().classes("w-full items-center gap-2 mt-1"):
            ui.label("🔑").classes("text-base shrink-0")
            pw_title = ui.label(
                _PROVIDERS[_sel_provider[0]]["pw_label"]
            ).classes("text-sm font-semibold text-yellow-300 shrink-0")
            pw_badge = ui.badge(
                "recommended" if _sel_provider[0] != "custom" else "",
                color="amber",
            ).classes("text-xs shrink-0")
            if _sel_provider[0] == "custom":
                pw_badge.style("display:none")

        smtp_password = ui.input(
            "",
            password=True,
            value=ecfg.get("smtp_password", "") or "",
            placeholder="Paste your App Password here (leave blank to keep existing)",
        ).classes("w-full")

        pw_hint = ui.label("").classes("text-xs text-gray-400 mt-1")

        def _set_pw_hint(provider: str):
            hints = {
                "gmail":   ("An App Password is a 16-character code generated at "
                            "myaccount.google.com. It is NOT your regular Google password."),
                "outlook": ("An App Password is generated at account.microsoft.com > Security. "
                            "It is NOT your regular Microsoft password."),
                "custom":  "Enter your regular SMTP password for this account.",
            }
            pw_hint.text = hints.get(provider, "")

        _set_pw_hint(_sel_provider[0])

        ui.separator().classes("my-3")
        ui.label("Addresses").classes("text-sm font-semibold text-gray-300 mb-1")

        with ui.row().classes("w-full items-center gap-1"):
            from_addr = ui.input(
                "From address",
                value=ecfg.get("from_addr", "") or "",
                placeholder="Same as your email address above",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "The sender address shown in the email. "
                "Must match your SMTP account address for most providers."
            )

        with ui.row().classes("w-full items-center gap-1"):
            to_addr = ui.input(
                "Send reports to",
                value=ecfg.get("to_addr", "") or "",
                placeholder="Where you want to receive the reports",
            ).classes("flex-1")
            ui.icon("info_outline", size="xs").classes("text-blue-400 cursor-help").tooltip(
                "Reports are delivered here. Can be the same as your From address "
                "or a different inbox."
            )

        # ── Provider change handler ───────────────────────────────────────────
        def _on_provider_change(e):
            p = e.value
            _sel_provider[0] = p
            preset = _PROVIDERS[p]
            smtp_host.value = preset["host"]
            smtp_port.value = preset["port"]
            use_tls.value   = preset["tls"]
            smtp_user.placeholder = preset["username_hint"]
            pw_title.text = preset["pw_label"]
            _set_pw_hint(p)
            if p != "custom":
                pw_badge.style("")
                pw_badge.text = "recommended"
            else:
                pw_badge.style("display:none")
            _build_howto(p)
            if p == "custom":
                howto_card.style("display:none")
            else:
                howto_card.style("background:#1c1a0e; border:1px solid #d97706;")

        provider_sel.on("update:model-value", _on_provider_change)

        # ── Buttons ───────────────────────────────────────────────────────────
        test_result = ui.label("").classes("text-sm mt-3 leading-relaxed")

        async def test_email():
            test_result.text = "Connecting to SMTP server..."
            test_result.classes(replace="text-sm mt-3 text-gray-400 leading-relaxed")
            try:
                from backend.src.services.notifications import email_service
                cfg_snap = {
                    "smtp_host":     smtp_host.value,
                    "smtp_port":     int(smtp_port.value or 587),
                    "smtp_user":     smtp_user.value,
                    "smtp_password": smtp_password.value or ecfg.get("smtp_password", ""),
                    "from_addr":     from_addr.value or smtp_user.value,
                    "to_addr":       to_addr.value,
                    "use_tls":       int(use_tls.value),
                }
                html = """<!DOCTYPE html><html><body
                  style="margin:0;padding:24px;background:#111827;font-family:Arial,sans-serif;">
                <p style="color:#f59e0b;font-size:22px;font-weight:bold;">FOREX Trader</p>
                <p style="color:#e5e7eb;font-size:15px;margin-top:12px;">
                  Your email settings are working correctly.
                  Daily and weekly trade summaries will be delivered to this address.
                </p>
                <p style="color:#6b7280;font-size:12px;margin-top:20px;">
                  This is a test message sent from FOREX Trader Email Reports.
                </p></body></html>"""
                ok, err = await email_service.send_email(
                    "FOREX Trader — Connection Test", html, cfg_snap
                )
                if ok:
                    test_result.text = (
                        f"✓  Test email sent successfully to {to_addr.value or smtp_user.value}"
                    )
                    test_result.classes(replace="text-sm mt-3 text-green-400 font-semibold leading-relaxed")
                    ui.notify("Test email sent!", type="positive")
                else:
                    friendly = _smtp_friendly_error(err)
                    test_result.text = friendly
                    test_result.classes(replace="text-sm mt-3 text-red-300 leading-relaxed whitespace-pre-line")
            except Exception as exc:
                friendly = _smtp_friendly_error(str(exc))
                test_result.text = friendly
                test_result.classes(replace="text-sm mt-3 text-red-400 leading-relaxed whitespace-pre-line")

        def save_smtp():
            updates: dict = {
                "smtp_host":  smtp_host.value or "",
                "smtp_port":  int(smtp_port.value or 587),
                "smtp_user":  smtp_user.value or "",
                "from_addr":  from_addr.value or smtp_user.value or "",
                "to_addr":    to_addr.value or "",
                "use_tls":    int(use_tls.value),
            }
            if smtp_password.value:
                updates["smtp_password"] = smtp_password.value
            settings_ctl.save_email_config(updates)
            ui.notify("SMTP settings saved", type="positive")

        with ui.row().classes("gap-3 mt-4 flex-wrap"):
            ui.button("Save Settings", icon="save", on_click=save_smtp).classes(
                "bg-blue-700 text-white px-5 py-2"
            )
            ui.button("Send Test Email", icon="send", on_click=test_email).classes(
                "bg-gray-600 text-white px-5 py-2"
            )
        test_result
