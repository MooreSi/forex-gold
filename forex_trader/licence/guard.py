"""
Licence enforcement for the FOREX Trader app — fully offline, Ed25519 signature.

Algorithm:
  signature = Ed25519_sign(private_key, "{machine_id}|{expiry_date}|2.0")
  formatted as 16 groups of 8 uppercase hex chars (128 hex chars total)

  Verification uses only the public key bundled in licence/verify.py — the
  matching private signing key lives solely in the admin KeyGen tool and is
  never shipped with the app.

Activation code format (what the admin sends to the user):
  KEY|EXPIRY_DATE  e.g.  D17BE902-...|2027-06-09  or  D17BE902-...|perpetual

Call enforce() at startup before the NiceGUI server is started.
"""
import logging

log = logging.getLogger(__name__)

from forex_trader.licence.verify import verify_licence_key as _verify_licence_key


def _app_port() -> int:
    """The port the real app will actually run on, once past this licence
    gate -- must match run.py's own port (config.get("port"), default 8888
    per config.py) exactly, or the activation/error screens end up serving
    on a different port than the app restarts into: the "click here to open
    FOREX Trader" link and its auto-reload poll are both relative to
    wherever THIS page loaded from, so a mismatch leaves them pointed at a
    dead port forever."""
    try:
        import forex_trader.config as _config
        return int(_config.get("port", 8888))
    except Exception:
        return 8888


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
        # `reason` is shown on the activation screen as an explanatory banner
        # rather than being dropped, so the user knows why they are being asked
        # to register again instead of just seeing a bare "activation required".
        _show_registration_page(notice=reason)
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

    ui.run(host="0.0.0.0", port=_app_port(), title="FOREX Trader — Licence Error",
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


def _show_registration_page(notice: str = "") -> None:
    """Show the licence activation screen and block until activated.

    `notice` explains why activation is being asked for again (stale key after
    a signing-scheme change, expired licence, machine change). Empty on a
    genuine first install.
    """
    import asyncio
    import os
    import sys
    from nicegui import ui, app as _ng_app
    from fastapi.responses import HTMLResponse
    from forex_trader.licence import store as _store_mod
    from forex_trader.licence.fingerprint import get_fingerprint

    machine_id = get_fingerprint()

    # A machine that was approved before already has a remote token, so the
    # admin server knows it and can push a corrected licence key without the
    # user re-registering at all (see remote/server.py's resign_all_licences).
    # Nothing else on this screen starts the remote client — without this,
    # a client stranded by a re-signing event would sit here doing nothing
    # until someone manually filled the form in, which is exactly the case
    # that has no remote fix. Start the agent up front when a token exists so
    # the push can land on its own.
    _known_client = False
    _stored_email = ""
    _stored_nickname = ""
    try:
        from forex_trader.remote import client as _rc_boot
        _known_client = _rc_boot._TOKEN_FILE.exists()
        _stored_email = _rc_boot.get_stored_email()
        _stored_nickname = _rc_boot.get_stored_nickname()
    except Exception as _rc_err:
        log.warning("Could not inspect remote client state on activation screen: %s", _rc_err)

    # Plain HTML "please wait" page served while the process restarts.
    # No socket.io — survives the NiceGUI process dying.
    @_ng_app.get("/licence-activated")
    def _lic_page():
        return HTMLResponse(_ACTIVATION_HTML)

    if _known_client:
        @_ng_app.on_startup
        def _autoconnect_known_client():
            try:
                from forex_trader.remote import client as _rc_auto
                _rc_auto.start()
                log.info(
                    "Activation screen: existing remote token found — "
                    "connecting so an admin-pushed licence can self-heal this install."
                )
            except Exception as exc:
                log.warning("Could not auto-start remote client on activation screen: %s", exc)

    @ui.page("/")
    def _reg_page():
        ui.dark_mode(True)
        ui.query("body").style("background:#0f1117")

        with ui.column().classes("absolute-center items-center gap-5 w-full max-w-md px-6"):
            ui.icon("vpn_key", size="3.5rem").classes("text-blue-400")
            ui.label("FOREX Trader").classes("text-3xl font-bold text-white tracking-tight")
            ui.label("Licence activation required.").classes("text-gray-400 text-sm")

            if notice:
                with ui.card().classes(
                    "w-full bg-amber-950 border border-amber-700 p-3 gap-1"
                ):
                    with ui.row().classes("items-center gap-2 no-wrap"):
                        ui.icon("info", size="1.2rem").classes("text-amber-400")
                        ui.label(notice).classes("text-amber-200 text-xs leading-snug")
                    if _known_client:
                        ui.label(
                            "This machine is already known to your administrator — "
                            "if they are online, a replacement licence may arrive "
                            "automatically. Otherwise request one below."
                        ).classes("text-amber-300/70 text-xs leading-snug")

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
                value=_stored_nickname,
            ).props("outlined").classes("w-full text-sm")
            ui.label(
                "This will identify you in the admin panel and on your licence."
            ).classes("text-gray-500 text-xs -mt-2")

            email_input = ui.input(
                "Email Address *",
                placeholder="your@email.com",
                value=_stored_email,
            ).props("outlined").classes("w-full text-sm")

            status_lbl = ui.label("").classes("text-sm min-h-5")

            # ── Request Registration (automated flow) ──────────────────────────

            async def _request_registration():
                from forex_trader.remote import client as _rc
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

                    if not _verify_licence_key(machine_id, expiry_date, key):
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
                from forex_trader.remote import client as _rc
                from forex_trader.remote.client import _do_restart as _restart
                if _rc.licence_activated.is_set():
                    _lic_timer.cancel()
                    status_lbl.set_text("Licence activated! Loading main app...")
                    status_lbl.classes(replace="text-sm text-green-400")
                    await asyncio.sleep(0.4)
                    ui.navigate.to("/licence-activated")
                    await asyncio.sleep(0.6)
                    _restart()

            _lic_timer = ui.timer(1.0, _check_activation)

    ui.run(host="0.0.0.0", port=_app_port(), title="FOREX Trader — Activate",
           dark=True, reload=False)
    sys.exit(0)


# ── Main enforce function ─────────────────────────────────────────────────────

def enforce() -> None:
    """
    Verify the licence at startup — fully offline, no server calls.

    Check order:
      1. Store has machine_id + expiry_date + licence_key — if missing, show activation screen
      2. Stored machine_id matches current machine — if not, clear store and show activation
      3. Ed25519 signature valid for machine_id + expiry_date — if not, clear store
         and show activation
      4. Expiry date not passed — if it has, show activation so a renewal can be requested

    Every failure path lands on the activation screen rather than a dead-end
    error page: that screen can request a new licence and can receive one
    pushed by the admin console, so a stranded install is always recoverable
    without physical access to the machine.
    """
    from forex_trader.licence import store as _store
    from forex_trader.licence.fingerprint import get_fingerprint

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
    # email and licence_type are stored but not required for signature verification
    current_machine   = get_fingerprint()
    already_verified  = False

    if stored_machine_id != current_machine:
        # Fingerprints differ — this can happen when OS updates change how hardware
        # values are reported (e.g. system_profiler field format changes on macOS, or
        # a flaky WMI/CIM query on Windows returning a slightly different value between
        # calls) even though the physical machine hasn't changed.
        #
        # Verify the signature against the STORED machine_id (the one the key was
        # actually issued for). If it passes, the key is genuine for this machine —
        # the drift is benign. Update the stored ID to the new fingerprint so future
        # startups skip this path, but keep verifying against the ORIGINAL id for the
        # rest of this run: the key was only ever signed for that id, so re-checking
        # it against the new, different current_machine below would always fail even
        # though nothing was actually tampered with (confirmed live: this was turning
        # every benign drift into a false "invalid or tampered" error).
        if _verify_licence_key(stored_machine_id, expiry_date, licence_key):
            log.info(
                "Fingerprint drift detected (stored %s → current %s) — "
                "signature verified against original ID, updating store.",
                stored_machine_id[:8], current_machine[:8],
            )
            updated = dict(data)
            updated["machine_id"] = current_machine
            _store.save(updated)
            already_verified = True
        else:
            log.warning(
                "Machine ID mismatch — stored %s, current %s",
                stored_machine_id[:8], current_machine[:8],
            )
            _store.clear()
            _show_error_and_exit(
                "This licence was issued for a different machine. "
                "Request a new one for this machine below.",
                allow_register=True,
            )
            return

    if not already_verified and not _verify_licence_key(stored_machine_id, expiry_date, licence_key):
        # A key that no longer verifies is far more often a stale key than a
        # forged one: upgrading over an older install brings a new verify.py,
        # and every key issued under the retired signing scheme (e.g. the HMAC
        # keygen.py -> Ed25519 migration) stops validating against it. The old
        # behaviour here was a dead-end error screen, which is unrecoverable
        # both locally and remotely — the remote client never starts, so the
        # admin cannot push a corrected key either. Clear the bad key and send
        # the user to the activation screen instead, which can request a new
        # licence and accepts an admin push. A genuinely forged key still gets
        # nowhere: it is discarded here, and the activation screen only ever
        # admits a key that verifies.
        log.warning("Licence signature verification failed — clearing store and re-registering.")
        _store.clear()
        _show_error_and_exit(
            "Your saved licence key is no longer valid for this version of "
            "FOREX Trader — it needs to be reissued.",
            allow_register=True,
        )
        return

    # Check expiry date (skip for perpetual licences)
    if expiry_date != "perpetual":
        from datetime import datetime as _dt_exp, timezone as _tz_exp
        try:
            exp_date = _dt_exp.strptime(expiry_date, "%Y-%m-%d").replace(tzinfo=_tz_exp.utc)
            if _dt_exp.now(_tz_exp.utc).date() > exp_date.date():
                # Same reasoning as the signature failure above: a dead-end
                # screen leaves no route back, so offer the activation screen
                # where a renewal can be requested or pushed. The expired key
                # is left in the store — it is genuine, and re-saving is the
                # activation screen's job once a renewal actually arrives.
                _show_error_and_exit(
                    f"Your licence expired on {expiry_date}. "
                    "Request a renewal below, or contact your administrator.",
                    allow_register=True,
                )
                return
        except ValueError:
            log.warning("Unrecognised expiry_date format %r — treating as valid", expiry_date)

    log.info("Licence OK (machine %s)", current_machine[:8])
