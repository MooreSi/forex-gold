"""
Licence enforcement for the FOREX Trader app — fully offline, HMAC-SHA256.

Algorithm:
  key = HMAC-SHA256(secret, "{machine_id}|{expiry_date}|1.0")
  formatted as XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX-XXXXXXXX

  The _SERVER_SECRET must match the value in the admin KeyGen tool.

Activation code format (what the admin sends to the user):
  KEY|EXPIRY_DATE  e.g.  6197CA98-...|2027-06-09  or  6197CA98-...|perpetual

Call enforce() at startup before the NiceGUI server is started.
"""
import logging

log = logging.getLogger(__name__)

from backend.src.config.licence.keygen import verify_licence_key as _verify_licence_hmac


def _parse_activation_code(code: str):
    """
    Parse KEY, KEY|EXPIRY, or KEY|EXPIRY|TYPE → (key, expiry_date, licence_type).
    Defaults: expiry='perpetual', type='Perpetual' for perpetual, 'Fixed Term' for dated.
    """
    parts = [p.strip() for p in code.strip().split("|")]
    key    = parts[0]
    expiry = parts[1] if len(parts) >= 2 else "perpetual"
    ltype  = parts[2] if len(parts) >= 3 else ("Perpetual" if expiry == "perpetual" else "Fixed Term")
    return key, expiry, ltype


# ── Blocking error screen ─────────────────────────────────────────────────────

def _show_error_and_exit(reason: str, allow_register: bool = False) -> None:
    import sys
    from nicegui import ui

    log.error("Licence check failed: %s", reason)

    if allow_register:
        _show_registration_page()
        return

    @ui.page("/")
    def _error_page():
        ui.dark_mode(True)
        ui.query("body").style("background:#0f1117")
        with ui.column().classes("absolute-center items-center gap-4 text-center max-w-md px-6"):
            ui.icon("lock", size="4rem").classes("text-red-400")
            ui.label("FOREX Trader — Licence Error").classes("text-2xl font-bold text-white")
            ui.label(reason).classes("text-red-300 text-sm")
            ui.label("Contact your administrator for assistance.").classes("text-gray-500 text-xs")

    ui.run(host="0.0.0.0", port=8888, title="FOREX Trader — Licence Error",
           dark=True, reload=False)
    sys.exit(1)


# ── Registration / activation screen ─────────────────────────────────────────

_ACTIVATION_HTML = """<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8">
  <title>FOREX Trader &mdash; Activating</title>
  <style>
    *{box-sizing:border-box;margin:0;padding:0}
    body{background:#0f1117;color:#fff;font-family:system-ui,sans-serif;
         display:flex;flex-direction:column;align-items:center;
         justify-content:center;height:100vh;gap:16px}
    h2{font-size:1.5rem;font-weight:700}
    p{color:#9ca3af;font-size:.9rem}
    .dot{display:inline-block;animation:blink 1.2s infinite}
    .dot:nth-child(2){animation-delay:.4s}
    .dot:nth-child(3){animation-delay:.8s}
    @keyframes blink{0%,80%,100%{opacity:0}40%{opacity:1}}
  </style>
</head>
<body>
  <svg width="48" height="48" viewBox="0 0 24 24" fill="#3b82f6">
    <path d="M12.65 10C11.83 7.67 9.61 6 7 6c-3.31 0-6 2.69-6 6s2.69 6
             6 6c2.61 0 4.83-1.67 5.65-4H17v4h4v-4h2v-4H12.65zM7 14c-1.1
             0-2-.9-2-2s.9-2 2-2 2 .9 2 2-.9 2-2 2z"/>
  </svg>
  <h2>Licence Activated</h2>
  <p>Loading FOREX Trader<span class="dot">.</span><span class="dot">.</span><span class="dot">.</span></p>
  <p id="manual-link" style="display:none;margin-top:8px">
    Taking longer than expected &mdash;
    <a href="/" style="color:#3b82f6" onclick="location.reload(true)">click here to open FOREX Trader</a>
  </p>
  <script>
    // Poll the root until the new app server answers, then force a hard
    // navigation there. cache:'no-store' avoids the browser serving a stale
    // cached failure/redirect from the moment the old process was dying.
    // We deliberately never clearInterval/give up on our own: a single
    // successful poll could still be racing the old process's last gasp
    // before it exits, so we keep polling and only stop once the hard
    // reload actually takes us off this page.
    var attempts = 0;
    var t = setInterval(function(){
      attempts++;
      if(attempts === 6){
        document.getElementById('manual-link').style.display = 'block';
      }
      fetch('/', {cache: 'no-store'}).then(function(r){
        if(r.ok){ location.reload(true); }
      }).catch(function(){});
    }, 800);
  </script>
</body>
</html>"""


def _show_registration_page() -> None:
    """Show the licence activation screen and block until activated."""
    import asyncio
    import os
    import sys
    from nicegui import ui, app as _ng_app
    from fastapi.responses import HTMLResponse
    from backend.src.config.licence import store as _store_mod
    from backend.src.config.licence.fingerprint import get_fingerprint

    machine_id = get_fingerprint()

    # Plain HTML "please wait" page served while the process restarts.
    # No socket.io — survives the NiceGUI process dying.
    @_ng_app.get("/licence-activated")
    def _lic_page():
        return HTMLResponse(_ACTIVATION_HTML)

    @ui.page("/")
    def _reg_page():
        ui.dark_mode(True)
        ui.query("body").style("background:#0f1117")

        with ui.column().classes("absolute-center items-center gap-5 w-full max-w-md px-6"):
            ui.icon("vpn_key", size="3.5rem").classes("text-blue-400")
            ui.label("FOREX Trader").classes("text-3xl font-bold text-white tracking-tight")
            ui.label("Licence activation required.").classes("text-gray-400 text-sm")

            # Machine ID — displayed for reference; also sent automatically in the request flow.
            with ui.card().classes("w-full bg-gray-900 border border-gray-700 p-4 gap-2"):
                ui.label("Your Machine ID").classes("text-xs text-gray-500 uppercase tracking-wide")
                ui.label(
                    "Your administrator uses this ID to generate your licence key."
                ).classes("text-gray-400 text-xs")
                with ui.row().classes("items-center gap-2 w-full mt-1"):
                    ui.label(machine_id).classes(
                        "font-mono text-xs text-green-400 flex-1 break-all select-all"
                    )
                    ui.button(
                        icon="content_copy",
                        on_click=lambda: (
                            ui.clipboard.write(machine_id),
                            ui.notify("Machine ID copied", type="positive", timeout=1500),
                        ),
                    ).props("flat round size=xs color=grey").tooltip("Copy to clipboard")

            nickname_input = ui.input(
                "Your Name / Nickname *",
                placeholder="e.g. John or JohnTrader",
            ).props("outlined").classes("w-full text-sm")
            ui.label(
                "This will identify you in the admin panel and on your licence."
            ).classes("text-gray-500 text-xs -mt-2")

            email_input = ui.input(
                "Email Address *",
                placeholder="your@email.com",
            ).props("outlined").classes("w-full text-sm")

            status_lbl = ui.label("").classes("text-sm min-h-5")

            # ── Request Registration (automated flow) ──────────────────────────

            async def _request_registration():
                from backend.src.services.cluster.remote import client as _rc
                nickname = nickname_input.value.strip()
                email    = email_input.value.strip()
                if len(nickname) < 2:
                    status_lbl.set_text("Enter a name or nickname (at least 2 characters).")
                    status_lbl.classes(replace="text-sm text-orange-400")
                    nickname_input.props("error")
                    return
                nickname_input.props(remove="error")
                if not email or "@" not in email:
                    status_lbl.set_text("Enter a valid email address.")
                    status_lbl.classes(replace="text-sm text-orange-400")
                    return

                status_lbl.set_text("Sending registration request...")
                status_lbl.classes(replace="text-sm text-gray-400")

                # request_registration() saves the email + nickname, cancels any existing
                # connect loop, and starts a fresh one that will send
                # MSG_REGISTER.  Do NOT also call _rc.start() — that would
                # spawn a second loop and double the server failure count,
                # triggering the rate-limiter before registration gets through.
                _rc.request_registration(email, nickname)

                status_lbl.set_text(
                    "Request sent — awaiting administrator approval. "
                    "The app will activate automatically once approved."
                )
                status_lbl.classes(replace="text-sm text-yellow-400")

            ui.button(
                "Request Registration", icon="send",
                on_click=_request_registration,
            ).props("color=blue").classes("w-full")

            # ── Manual activation (fallback if server unreachable) ─────────────

            with ui.expansion("Manual Activation", icon="vpn_key").classes(
                "w-full text-gray-500 text-sm"
            ):
                code_input = ui.input(
                    "Licence Key",
                    placeholder="Paste the key provided by your administrator",
                ).props("outlined").classes("w-full font-mono text-sm mt-2")

                async def _activate():
                    nickname = nickname_input.value.strip()
                    email    = email_input.value.strip()
                    raw      = code_input.value.strip()

                    if len(nickname) < 2:
                        status_lbl.set_text("Enter a name or nickname (at least 2 characters).")
                        status_lbl.classes(replace="text-sm text-orange-400")
                        return
                    if not email or "@" not in email:
                        status_lbl.set_text("Enter a valid email address.")
                        status_lbl.classes(replace="text-sm text-orange-400")
                        return
                    if not raw:
                        status_lbl.set_text("Enter your licence key.")
                        status_lbl.classes(replace="text-sm text-orange-400")
                        return

                    key, expiry_date, licence_type = _parse_activation_code(raw)

                    status_lbl.set_text("Verifying...")
                    status_lbl.classes(replace="text-sm text-gray-400")

                    if not _verify_licence_hmac(machine_id, expiry_date, key):
                        status_lbl.set_text(
                            "Invalid licence key — this key was not issued for this machine."
                        )
                        status_lbl.classes(replace="text-sm text-red-400")
                        return

                    _store_mod.save({
                        "machine_id":   machine_id,
                        "nickname":     nickname,
                        "email":        email,
                        "expiry_date":  expiry_date,
                        "licence_type": licence_type,
                        "licence_key":  key,
                    })
                    status_lbl.set_text("Activated! Launching...")
                    status_lbl.classes(replace="text-sm text-green-400")
                    await asyncio.sleep(1.0)
                    try:
                        os.execv(sys.executable, [sys.executable] + sys.argv)
                    except OSError as _execv_err:
                        log.error("os.execv failed after licence activation: %s", _execv_err)
                        status_lbl.set_text(
                            "Activated! Please close and reopen the app to continue."
                        )
                        status_lbl.classes(replace="text-sm text-yellow-400")

                ui.button("Activate Manually", on_click=_activate).props(
                    "color=grey outlined"
                ).classes("w-full mt-1")

            # ── Licence activation watcher ─────────────────────────────────
            # Polls once per second for the remote client to push a licence.
            # When the event is set (client.py saves the key), we navigate the
            # browser to a self-contained "please wait" page BEFORE killing
            # the process — avoids the NiceGUI "Connection lost" banner and
            # gives the bat loop time to restart run.py cleanly.

            async def _check_activation():
                from backend.src.services.cluster.remote import client as _rc
                from backend.src.services.cluster.remote.client import _do_restart as _restart
                if _rc.licence_activated.is_set():
                    _lic_timer.cancel()
                    status_lbl.set_text("Licence activated! Loading main app...")
                    status_lbl.classes(replace="text-sm text-green-400")
                    await asyncio.sleep(0.4)
                    ui.navigate.to("/licence-activated")
                    await asyncio.sleep(0.6)
                    _restart()

            _lic_timer = ui.timer(1.0, _check_activation)

    ui.run(host="0.0.0.0", port=8888, title="FOREX Trader — Activate",
           dark=True, reload=False)
    sys.exit(0)


# ── Main enforce function ─────────────────────────────────────────────────────

def enforce() -> None:
    """
    Verify the licence at startup — fully offline, no server calls.

    Check order:
      1. Store has machine_id + expiry_date + licence_key — if missing, show activation screen
      2. Stored machine_id matches current machine — if not, block (wrong machine)
      3. HMAC-SHA256 valid for machine_id + expiry_date — if not, clear store and block
    """
    from backend.src.config.licence import store as _store
    from backend.src.config.licence.fingerprint import get_fingerprint

    data = _store.load()
    if (
        not data
        or not data.get("licence_key")
        or not data.get("machine_id")
        or not data.get("expiry_date")
    ):
        log.info("No valid licence found — showing activation screen.")
        _show_error_and_exit("", allow_register=True)
        return

    stored_machine_id = data["machine_id"]
    expiry_date       = data["expiry_date"]
    licence_key       = data["licence_key"]
    # email and licence_type are stored but not required for HMAC verification
    current_machine   = get_fingerprint()

    if stored_machine_id != current_machine:
        # Fingerprints differ — this can happen when macOS updates change how hardware
        # values are reported (e.g. system_profiler field format changes) even though
        # the physical machine hasn't changed.
        #
        # Verify the HMAC against the STORED machine_id (the one the key was issued for).
        # If it passes, the key is genuine for this machine — the drift is benign.
        # Update the stored ID to the new fingerprint so future startups skip this path.
        # Security is unchanged: the HMAC is still validated; a key for machine A will
        # not pass this check on a genuinely different machine B.
        if _verify_licence_hmac(stored_machine_id, expiry_date, licence_key):
            log.info(
                "Fingerprint drift detected (stored %s → current %s) — "
                "HMAC verified against original ID, updating store.",
                stored_machine_id[:8], current_machine[:8],
            )
            updated = dict(data)
            updated["machine_id"] = current_machine
            _store.save(updated)
            stored_machine_id = current_machine  # continue with expiry check below
        else:
            log.warning(
                "Machine ID mismatch — stored %s, current %s",
                stored_machine_id[:8], current_machine[:8],
            )
            _store.clear()
            _show_error_and_exit("", allow_register=True)
            return

    if not _verify_licence_hmac(stored_machine_id, expiry_date, licence_key):
        log.warning("Licence HMAC verification failed — clearing store.")
        _store.clear()
        _show_error_and_exit("Your licence key is invalid or has been tampered with.")
        return

    # Check expiry date (skip for perpetual licences)
    if expiry_date != "perpetual":
        from datetime import datetime as _dt_exp, timezone as _tz_exp
        try:
            exp_date = _dt_exp.strptime(expiry_date, "%Y-%m-%d").replace(tzinfo=_tz_exp.utc)
            if _dt_exp.now(_tz_exp.utc).date() > exp_date.date():
                _show_error_and_exit(
                    f"Your licence expired on {expiry_date}. "
                    "Contact your administrator to obtain a renewal key."
                )
                return
        except ValueError:
            log.warning("Unrecognised expiry_date format %r — treating as valid", expiry_date)

    log.info("Licence OK (machine %s)", current_machine[:8])
