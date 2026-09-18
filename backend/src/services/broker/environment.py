"""Which account the whole app is pointed at: the demo one, or the live one.

**This is the control that points the app at real money.** Every other setting
decides what happens on whichever account is selected; this decides which
account that is. It was a toggle in the NiceGUI header and the React port
dropped it, so an install could only be moved between environments by editing
`config.yaml` and restarting.

Four things have to happen together, or the app ends up half-switched — reading
one account's trade history while sending orders to the other, with neither
screen saying so:

  1. the target account's credentials are written to `bridge_credentials.json`,
     which is what the bridge reads when it starts;
  2. the shared database connection is re-pointed at that environment's file;
  3. `account_env` is persisted, so a restart comes back to the same place;
  4. the app restarts, so every cached handle — the runtime, the bridge, the
     engines — is rebuilt against the new account.

**The order is the safety property**, and the first three live here. Nothing is
written until the target's credentials have been checked: a database pointed at
`live` while the bridge is still logged into the demo account is the exact
failure this sequencing exists to prevent.

The restart is step four and is the caller's, so that "what changed" and "the
app went away" are separate, testable things.

**Restart rather than an in-place bridge reconnect.** The NiceGUI version told
the running bridge to change account, with a long tail of handling for a
reconnect that half-worked, an older bridge build, or autotrading that would
not re-enable. A restart makes the switch atomic and needs nothing past the
runtime facade. It costs a few seconds and removes a class of half-switched
states; the in-place version can be added later if those seconds matter, and
would need `send_credentials`/`reconnect`/`enable_autotrading` on the facade.

Credentials for BOTH accounts live in the demo database, deliberately: they
have to be readable while pointing at either environment, and a per-environment
copy is one that goes stale on whichever side was not edited.
"""
from __future__ import annotations

import logging

import backend.src.config as _cfg
from backend.src.services.broker import credentials_repo as _creds
from backend.src.services.risk import retention as _retention

log = logging.getLogger(__name__)

__all__ = ["current", "describe", "switch"]

ENVIRONMENTS = ("demo", "live")
DEFAULT = "demo"

# The credential column names differ per environment; the demo one has no
# prefix because it came first.
_FIELDS = {
    "demo": ("login", "password_enc", "server"),
    "live": ("live_login", "live_password_enc", "live_server"),
}


def _db_path_for(environment: str) -> str:
    from backend.src.config import DATA_DIR

    return str(DATA_DIR / f"forex_trader_{environment}.db")


def current() -> str:
    """The stored environment, defaulting to demo.

    **Fails safe.** An unset value, or one this build does not recognise,
    reads as demo: a typo in `config.yaml` is not a reason to point an app at
    a live account.
    """
    stored = (_cfg.get("account_env", DEFAULT) or DEFAULT).strip().lower()
    return stored if stored in ENVIRONMENTS else DEFAULT


def _account(environment: str) -> tuple[str, str, str]:
    login_f, password_f, server_f = _FIELDS[environment]
    creds = _creds.get_mt5_credentials() or {}
    return (str(creds.get(login_f) or "").strip(),
            str(creds.get(password_f) or ""),
            str(creds.get(server_f) or "").strip())


def describe() -> dict:
    """Which environment is active, and whether each one can be switched to.

    The login and server are reported so an operator can check they are about
    to switch to the account they meant. The password is not.
    """
    out = {}
    for environment in ENVIRONMENTS:
        login, password, server = _account(environment)
        out[environment] = {
            "login": login,
            "server": server,
            "configured": bool(login and password and server),
        }
    return {"current": current(), "environments": out}


def switch(environment: str) -> dict:
    """Point the app at `environment`. Does NOT restart — see the docstring.

    Raises ValueError, with a message for the operator, rather than leaving a
    half-switched app behind.
    """
    environment = (environment or "").strip().lower()
    if environment not in ENVIRONMENTS:
        raise ValueError(
            f"Unknown environment {environment!r}. "
            f"Known: {', '.join(ENVIRONMENTS)}.")

    label = environment.capitalize()
    login, password, server = _account(environment)
    if not (login and password and server):
        raise ValueError(
            f"No {label} MT5 credentials are saved. Enter the {label} account's "
            "login, password and server under Settings > MT5, then switch again."
        )

    # FIRST, because it is the step that can still fail for reasons the check
    # above cannot see — a read-only directory, a missing bridge folder. A
    # database re-pointed before this, with the file unwritten, is the
    # half-switch.
    if not _creds.sync_bridge_credentials_file(environment):
        raise ValueError(
            f"The {label} credentials could not be written to the bridge's "
            "credentials file, so the bridge would come back up on the old "
            "account. Nothing was changed.")

    _retention.switch_environment(_db_path_for(environment))
    _cfg.save_to_yaml({"account_env": environment})
    log.warning("[env] switched to %s (%s on %s)", environment, login, server)

    return {
        "environment": environment,
        "is_live": environment == "live",
        "login": login,
        "server": server,
        "restart_required": True,
        "note": (
            f"Now pointed at the {label} account {login} on {server}. "
            "Make sure MetaTrader 5 is logged into that account before trading "
            "resumes."
        ),
    }
