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
  4. the app restarts, so every cached handle is rebuilt against the new
     account — and, because the bridge is NOT one of those handles, the app
     tells it which account to log into on the way back up (`align_bridge`).

**The order is the safety property**, and the first three live here. Nothing is
written until the target's credentials have been checked: a database pointed at
`live` while the bridge is still logged into the demo account is the exact
failure this sequencing exists to prevent.

The restart is step four and is the caller's, so that "what changed" and "the
app went away" are separate, testable things.

**A restart alone does not move the bridge, and for six weeks that was this
module's central mistake.** The NiceGUI version told the running bridge to
change account; the React port replaced that with the restart, on the
reasoning that a restart rebuilds every cached handle and so needs nothing
past the runtime facade. It rebuilds every handle THIS PROCESS owns. The
bridge is a separate process — under Wine on a Mac, with the MT5 terminal
behind it — and `run._start_mt5_bridge` deliberately leaves one that is
already listening alone, because a Wine relaunch tears down wineserver and
every child. So the switch wrote the live credentials, the app came back on
the live database, and the terminal stayed logged into demo (owner's Mac,
2026-09-22). `align_bridge` below is the missing half of step four: the app
tells the bridge, on the way up, when the two disagree.

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

__all__ = ["align_bridge", "current", "describe", "switch"]

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
        # Three states, not two. A password that is SAVED but cannot be
        # decrypted reaches `_account` as "" and looks identical to one that
        # was never entered -- and the advice for the two is opposite. On
        # 2026-09-19 the header's switch was disabled with "Add it under
        # Settings > MT5" on an install where both accounts were fully
        # configured: the app was running under an interpreter with no
        # `keyring`, so secrets fell back to a key file, the key did not match
        # and every password decrypted to "". Re-typing them would not have
        # helped.
        #
        # An account with a login and a server but no readable password is the
        # signature: nothing was stored, nothing would decrypt to "".
        unreadable = bool(login and server and not password)
        out[environment] = {
            "login": login,
            "server": server,
            "configured": bool(login and password and server),
            "unreadable": unreadable,
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
            "MetaTrader 5 is logged into that account as the app comes back "
            "up; the header says so if it could not be."
        ),
    }


async def align_bridge(bridge) -> dict:
    """Make the RUNNING bridge log into the account this app is pointed at.

    **The step the restart does not perform.** `switch` writes the target's
    credentials to `bridge_credentials.json` and the app restarts, on the
    stated assumption that a restart rebuilds "every cached handle — the
    runtime, the bridge, the engines". That is only true of handles this
    process owns. On a Mac the bridge is a Wine subprocess with the MT5
    terminal behind it, and `run._start_mt5_bridge` deliberately leaves a
    bridge that is already listening alone (a Wine relaunch tears down
    wineserver and every child, which would turn a restart into an outage).
    It reads its credentials once, when it connects, and never again.

    So the bridge outlived the restart on the account the app had just left:
    config, database and orders on live, the terminal still on demo.
    Confirmed on the owner's Mac 2026-09-22 — bridge up since 10:12, app
    restarted at 17:17, `bridge_credentials.json` correctly holding the live
    account, MT5 never moved. The NiceGUI app never had this bug because it
    re-pointed the bridge in place (`telegram/bot_infra.cmd_switch_env`,
    still does); the React port swapped that for the restart.

    Three rules, and each of them cost something to learn:

    * **Only on a genuine disagreement.** A re-login costs a terminal round
      trip and resets AutoTrading. Doing it on every boot would pay that on
      every restart.
    * **Silence is not disagreement.** A bridge that answers nothing gets
      nothing sent to it: a cold MT5 can take ~150s to answer, and logging a
      guess in is how a restart becomes an outage.
    * **Verify from the bridge, not from the reply.** The status this returns
      names the account the bridge says it is on afterwards, because "the
      accounts agree" is the one claim that must not be taken on trust.

    Never raises. The caller is a startup path; an app that will not start is
    worse than one whose badge disagrees with its terminal — and the header's
    mismatch marker says so out loud.
    """
    environment = current()
    login, password, server = _account(environment)
    if not (login and password and server):
        return {"status": "skipped", "environment": environment,
                "reason": f"no {environment} credentials are readable"}

    async def _bridge_login() -> str:
        try:
            account = await bridge.get_account()
        except Exception as exc:
            log.warning("[env] could not read the bridge's account: %s", exc)
            return ""
        return str((account or {}).get("login") or "").strip()

    before = await _bridge_login()
    if not before:
        return {"status": "unknown", "environment": environment,
                "expected_login": login,
                "reason": "the bridge did not say which account it is on"}
    if before == login:
        return {"status": "aligned", "environment": environment,
                "expected_login": login, "bridge_login": before}

    log.warning("[env] the app is pointed at %s (%s) but the bridge is on %s "
                "— re-pointing it", environment, login, before)
    try:
        await bridge.send_credentials(int(login), password, server)
    except Exception as exc:
        log.error("[env] re-pointing the bridge at %s failed: %s", login, exc)
        return {"status": "failed", "environment": environment,
                "expected_login": login, "bridge_login": before,
                "reason": str(exc)}

    after = await _bridge_login()
    if after != login:
        log.error("[env] the bridge is still on %s, not %s — MetaTrader 5 and "
                  "this app are pointed at different accounts", after or "?", login)
        return {"status": "failed", "environment": environment,
                "expected_login": login, "bridge_login": after or before}
    log.warning("[env] the bridge is now on %s (%s)", login, server)
    return {"status": "repointed", "environment": environment,
            "expected_login": login, "bridge_login": after}
