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

from backend.src.config.licence.verify import verify_licence_key as _verify_licence_key


def _app_port() -> int:
    """The port the real app will actually run on, once past this licence
    gate -- must match run.py's own port (config.get("port"), default 8888
    per config.py) exactly, or the activation/error screens end up serving
    on a different port than the app restarts into: the "click here to open
    FOREX Trader" link and its auto-reload poll are both relative to
    wherever THIS page loaded from, so a mismatch leaves them pointed at a
    dead port forever."""
    try:
        import backend.src.config as _config
        return int(_config.get("port", 8888))
    except Exception:
        return 8888


def _parse_activation_code(code: str):
    """KEY, KEY|EXPIRY or KEY|EXPIRY|TYPE -> (key, expiry_date, licence_type).

    Delegates to `activation.parse_activation_code`, which is where the manual
    activation path reads it too. Two copies of this would be two answers to
    "what does a bare key default its expiry to".
    """
    from backend.src.config.licence.activation import parse_activation_code

    return parse_activation_code(code)


# ── What the activation screen brings up behind it ───────────────────────────
#
# Hit for real on 2026-09-02. The owner's Mac lost its licence file, restarted,
# and could not get back in:
#
#   1. no licence, so enforce() shows this screen and startup stops here --
#      run.py calls it BEFORE backend.src.app.startup();
#   2. the screen starts the remote CLIENT so an admin can push a licence down;
#   3. the client dials the admin server;
#   4. the admin server IS this machine, and it is started from startup(),
#      which step 1 never reached;
#   5. connection refused, for ever.
#
# ADMIN_AVAILABLE and password_is_set() were both true throughout. The issuer
# simply never ran, because the issuer would not start until it was licensed.
#
# This does not grant, weaken or bypass a licence. It starts the thing that can
# legitimately issue one, on the one machine allowed to.


def _this_is_the_admin_machine() -> bool:
    """Is this the machine that issues licences?

    Deliberately answered from the filesystem rather than by importing
    `backend.src.app.ADMIN_AVAILABLE` and
    `services.cluster.remote.auth.password_is_set`. `config/` sits at the
    bottom of the stack and the import contract holds it there -- reaching up
    to the composition root from here would make the module that gates
    startup depend on the module that performs it.

    The facts are the same ones `app.py` uses, checked directly:
      * this is the licence-issuer machine by hardware (licence/issuer.py);
      * KeyGen's admin module is present next to FOREX or in ~/Documents;
      * an admin password has been set (remote/admin_password.hash, non-empty).

    The hardware pin leads because the other two are not evidence on their own
    (2026-09-12): `~/Documents` is iCloud-synced, so KeyGen arrives on every Mac
    on the owner's Apple ID, and a stale password hash never expires. A client
    that answers True here brings up the ISSUER behind the activation screen
    instead of the client that would fetch it a licence.

    Never raises: this decides which agent the activation screen brings up,
    and that screen is the only way back in.
    """
    try:
        from pathlib import Path as _P

        from backend.src.config import USER_DATA_DIR as _udd
        from backend.src.config.licence.issuer import is_licence_issuer_machine

        if not is_licence_issuer_machine():
            return False
        forex_root = _P(__file__).parent.parent.parent.parent
        keygen = any((c / "forex_admin.py").exists() for c in (
            forex_root.parent / "KeyGen", _P.home() / "Documents" / "KeyGen"))
        if not keygen:
            return False
        pw = _P(_udd) / "remote" / "admin_password.hash"
        return pw.exists() and pw.stat().st_size > 0
    except Exception as exc:
        log.warning("Could not determine whether this is the admin machine: %s", exc)
        return False


# Agents injected by run.py, started only on the admin path (bugs/021).
#
# The Telegram approval poller has to live in services/telegram, and config/
# sits at the bottom of the import stack -- importing it here would be the
# lowest layer reaching upward, which the import contract exists to stop. So
# run.py, which is above both, hands the callable down instead.
_activation_agents: list = []


def register_activation_agent(fn) -> None:
    """Register something for the activation screen to start on the ADMIN
    machine. Used for the Telegram registration-approval poller: without it the
    Approve button in the alert has nothing polling for it and does nothing."""
    _activation_agents.append(fn)


def _start_activation_agents() -> None:
    """Run every registered agent, independently. One that raises must not stop
    the next, and must not take the screen down -- this screen is the only way
    back into a stranded install."""
    for fn in _activation_agents:
        try:
            fn()
        except Exception as exc:
            log.warning("Activation agent %s failed to start: %s",
                        getattr(fn, "__name__", fn), exc)


def _agents_for_activation(is_admin: bool, known_client: bool) -> list:
    """The callables that bring up whichever remote agent can get this machine
    relicensed. Returned rather than registered, so the server that runs them
    is the caller's choice and this function can be tested without one.

    The admin machine starts its SERVER -- the console can then issue a licence
    to itself, and the existing self-heal push installs it. It does not also
    start the client: the admin Mac does not connect to itself, and doing both
    would have it dial its own port and log a refusal on every retry.

    Everything else starts the client, exactly as before, so an admin-pushed
    licence can self-heal the install.

    Failures are swallowed by the runner. This screen is the only way back in,
    and an exception there replaces it with a traceback and strands the machine.
    """
    if is_admin:
        def _autostart_admin_server():
            from backend.src.services.cluster.remote import server as _rs_auto
            _rs_auto.start()
            log.info(
                "Activation screen: this is the admin machine — starting the "
                "admin server so a licence can be issued to it locally."
            )

        def _autostart_activation_agents():
            # Admin only. A client machine cannot issue a licence, and a poller
            # there would compete with the real admin for the bot token.
            _start_activation_agents()

        return [_autostart_admin_server, _autostart_activation_agents]

    if known_client:
        def _autoconnect_known_client():
            from backend.src.services.cluster.remote import client as _rc_auto
            _rc_auto.start()
            log.info(
                "Activation screen: existing remote token found — "
                "connecting so an admin-pushed licence can self-heal this install."
            )

        return [_autoconnect_known_client]

    return []


# ── Blocking error screen ─────────────────────────────────────────────────────

def _show_error_and_exit(reason: str, allow_register: bool = False) -> None:
    import sys

    # Only log an error when there is one. A first install reaches here with an
    # empty reason on the completely normal path -- enforce() has already said
    # "No valid licence found — showing activation screen" at INFO -- and the
    # bare `ERROR ... Licence check failed:` that followed it is the first
    # thing a user points at when the activation flow does not finish.
    if reason:
        log.error("Licence check failed: %s", reason)

    if allow_register:
        # `reason` is shown on the activation screen as an explanatory banner
        # rather than being dropped, so the user knows why they are being asked
        # to register again instead of just seeing a bare "activation required".
        _show_registration_page(notice=reason)
        return

    import uvicorn

    from backend.src.config.licence.activation_server import build_app

    log.info("Serving the licence error screen on port %s.", _app_port())
    uvicorn.run(build_app("", error=reason), host="0.0.0.0", port=_app_port(),
                log_level="warning", access_log=False)
    sys.exit(1)


# ── Registration / activation screen ─────────────────────────────────────────

# The wait page, its probe path and the activation form now live in
# `activation_server.py` — one module that renders every pre-boot screen,
# rather than a template here and a page builder there. `_ACTIVATION_HTML`,
# `_activation_html`, `_activation_probe` and `_ACTIVATION_PROBE_PATH` went
# with them on 2026-09-18.


def _show_registration_page(notice: str = "") -> None:
    """Show the licence activation screen and block until activated.

    `notice` explains why activation is being asked for again (stale key after
    a signing-scheme change, expired licence, machine change). Empty on a
    genuine first install.
    """
    import sys

    from backend.src.config.licence.fingerprint import get_fingerprint

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
    try:
        from backend.src.services.cluster.remote import client as _rc_boot
        _known_client = _rc_boot._TOKEN_FILE.exists()
    except Exception as _rc_err:
        log.warning("Could not inspect remote client state on activation screen: %s", _rc_err)

    agents = _agents_for_activation(
        is_admin=_this_is_the_admin_machine(), known_client=_known_client)

    import uvicorn

    from backend.src.config.licence.activation_server import build_app

    log.info("Serving the licence activation screen on port %s "
             "(machine %s, %s).", _app_port(), machine_id,
             "known client" if _known_client else "first registration")
    uvicorn.run(
        build_app(machine_id, notice=notice, on_startup=agents),
        host="0.0.0.0", port=_app_port(), log_level="warning", access_log=False,
    )
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
