"""Settings tab — the domains behind one tab, and the money ones are named.

The two outbound connections (email, Telegram) live in `notifications.py`,
which also owns the test sends that prove them. They kept their
`/api/settings/...` paths; only the module moved.

Split into domain endpoints rather than one `PUT /settings`, for the reason the
frontend conventions give: `settings.py` reached 3,112 lines because everything
that needed a setting was added to one surface. A per-domain endpoint keeps the
React side honest too — one `*Tab.tsx` per domain, each reading its own shape.

**Risk and MT5 touch money.** Not by placing an order — nothing here does —
but by changing what the engines are allowed to do next time and which account
they do it on. Both echo back what was actually stored, so the operator sees
the value the engine will use rather than the one they typed.

Credentials are write-only through this layer: `GET /mt5` reports whether
credentials exist and for which login, never the password.
"""
from __future__ import annotations

import sys

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api import auth as auth_gate
from backend.src.api.errors import Refusal
from backend.src.controllers import broker_controller as broker_ctl
from backend.src.api.redaction import redacted as _redacted
from backend.src.controllers import environment_controller as env_ctl
from backend.src.controllers import settings_controller as settings_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Named once: the popup tells the operator which file to open in MetaEditor.
EA_FILE_NAME = "ForexTraderBridge.mq5"

class ConfigWrite(BaseModel):
    model_config = {"extra": "allow"}


class RetentionWrite(BaseModel):
    days: int


class ExpertParamsWrite(BaseModel):
    values: dict


class AccessWrite(BaseModel):
    auto_login: bool


class Mt5Credentials(BaseModel):
    login: str
    password: str
    server: str
    # Which account these belong to. Both are stored in one row, so the field
    # names differ rather than the table.
    environment: str = "demo"


@router.get("/risk")
async def risk() -> dict:
    return settings_ctl.get_risk_settings()


@router.put("/risk")
async def update_risk(body: ConfigWrite) -> dict:
    """Write risk settings and read them back.

    The echo is the point: the risk service clamps and normalises, so what was
    typed and what the engine will use are not always the same number.
    """
    settings_ctl.update_risk_settings(dict(body.model_dump()))
    return settings_ctl.get_risk_settings()


@router.get("/app")
async def app_config() -> dict:
    """The config.yaml values, with secrets redacted."""
    return _redacted(settings_ctl.load_config())


@router.put("/app")
async def update_app_config(body: ConfigWrite) -> dict:
    settings_ctl.save_config(dict(body.model_dump()))
    return _redacted(settings_ctl.load_config())


@router.get("/mt5")
async def mt5() -> dict:
    """Which account is configured. **Never the password.**"""
    return _redacted(settings_ctl.get_mt5_credentials() or {})


@router.put("/mt5")
async def save_mt5(body: Mt5Credentials) -> dict:
    """Store one account's MT5 credentials and push them to the bridge's file.

    **One dict, under the column names the store uses.** It was three
    positional arguments to a one-dict function until 2026-09-18, so every save
    raised and MT5 credentials could not be set from the dashboard at all. The
    password column is `password_enc`, which is also what the repo encrypts on
    the way in — a value written as `password` would miss both.

    `environment` says which account. Both live in the same row of the master
    database, deliberately: they have to be readable while the app is pointed
    at either one, and a per-environment copy goes stale on whichever side was
    not edited.

    The bridge file is rewritten too, always: credentials saved and not synced
    leave the bridge authenticating as the previous account, which is the same
    shape of bug as backing up the wrong database — it looks like it worked.
    """
    environment = (body.environment or "demo").strip().lower()
    if environment not in ("demo", "live"):
        raise Refusal(f"Unknown environment {body.environment!r}.", status_code=400)

    prefix = "live_" if environment == "live" else ""
    updates = {
        f"{prefix}login": body.login,
        f"{prefix}password_enc": body.password,
        f"{prefix}server": body.server,
    }
    settings_ctl.save_mt5_credentials(updates)

    # Only when this IS the account the app is pointed at. Rewriting the file
    # after editing the OTHER account's credentials would hand the bridge an
    # account nobody asked it to use.
    if environment == env_ctl.describe_environments()["current"]:
        settings_ctl.sync_bridge_credentials_file(environment)
    return _redacted(settings_ctl.get_mt5_credentials() or {})


@router.get("/retention")
async def retention() -> dict:
    return {"days": settings_ctl.get_data_retention_days()}


@router.put("/retention")
async def set_retention(body: RetentionWrite) -> dict:
    if body.days < 1:
        raise Refusal("Data retention must be at least one day.", status_code=400)
    settings_ctl.set_data_retention_days(body.days)
    return {"days": settings_ctl.get_data_retention_days()}


@router.get("/expert-params")
async def expert_params() -> dict:
    """The generic tunables screen.

    Rendered from the catalogue, never hand-written per parameter: `/add-tunable`
    exists so a new tunable appears here with no UI change, and a hand-written
    form per parameter defeats it.
    """
    return settings_ctl.get_expert_param_catalogue()


@router.put("/expert-params")
async def save_expert_params(body: ExpertParamsWrite) -> dict:
    return settings_ctl.save_expert_params(body.values)


@router.post("/expert-params/reset")
async def reset_expert_params(body: ConfigWrite) -> dict:
    """Reset one parameter, or all of them when no key is given."""
    key = dict(body.model_dump()).get("key")
    if key:
        return settings_ctl.reset_expert_param(str(key))
    return settings_ctl.reset_all_expert_params()


@router.get("/access")
async def access() -> dict:
    """Whether this machine asks for the dashboard password on restart.

    Its own endpoint rather than a field on `/app`, for the reason the NiceGUI
    tab was its own tab: "does this machine ask for a password" is the first
    thing somebody looks for when they want to change it, and an access control
    buried in a page of unrelated settings is one that stays forgotten.
    """
    auto = bool(settings_ctl.get_config(auth_gate.SETTING_KEY, False))
    return {
        "auto_login": auto,
        # Said by the backend, not composed in the browser: it is the one thing
        # the operator needs to weigh, and a UI that forgot to render it would
        # be offering the choice without the consequence.
        "warning": (
            "Anyone who can open this machine can place and close live trades "
            "without a password." if auto else ""
        ),
    }


@router.put("/access")
async def set_access(body: AccessWrite) -> dict:
    """Turn the password prompt on or off.

    Nothing here weakens the gate itself: `auto_login_enabled` defaults to
    False and an unreadable config still keeps the door shut. This only writes
    the setting that gate reads.
    """
    settings_ctl.save_config({auth_gate.SETTING_KEY: bool(body.auto_login)})
    return await access()


@router.get("/diagnostics")
async def diagnostics() -> dict:
    """What the Diagnostics panel shows: the log since this app started, and
    the circuit breaker."""
    return {
        "log": [list(line) for line in await settings_ctl.live_log_lines()],
        "circuit_breaker": await settings_ctl.get_circuit_breaker_state_async(),
    }


@router.get("/ea")
async def ea_status() -> dict:
    """What the EA badge's popup shows. Reads only; installs nothing.

    `binary_shipped` is the difference between one click and one click plus
    F7: with a pre-compiled `.ex5` beside the source there is nothing for
    MetaEditor to do.
    """
    stale, detail = broker_ctl.ea_build_status()
    return {
        "stale": stale,
        "detail": detail,
        "binary_shipped": broker_ctl.ea_binary_is_shipped(),
        "platform": sys.platform,
    }


@router.post("/ea/install")
async def ea_install() -> dict:
    """Put the EA this app ships into every MetaTrader on this machine.

    Copies files. It opens, closes and sizes nothing -- but it does change
    which rules an already-attached EA will manage open positions with, once
    the terminal reloads the new build, so it is a POST that reports exactly
    what it did.

    **It never claims a compile it did not perform.** MetaEditor exits 0 on a
    build it never ran, so `compile_ea` refuses outright on macOS and verifies
    the `.ex5` is genuinely newer on Windows. When a compile is still needed
    the answer says so and names the key to press, rather than reporting a
    success that would leave the old build running silently -- which is the
    failure this whole area exists to end.
    """
    report = broker_ctl.ea_deploy_report()
    if not report.get("targets"):
        raise Refusal(
            "No MetaTrader terminal was found on this machine, so there is "
            "nothing to install the EA into.")

    needs_compile = int(report.get("needs_compile") or 0) > 0
    compiled = False
    next_step = ""

    if needs_compile:
        result = broker_ctl.ea_compile(report["targets"][0])
        compiled = bool(result.get("ok"))
        if compiled:
            needs_compile = False
            next_step = ("The EA was rebuilt. The chart reloads it by itself, "
                         "so there is nothing left to do.")
        else:
            next_step = (
                f"Open MetaEditor, open {EA_FILE_NAME} and press F7 to compile "
                f"it. The chart reloads the new build by itself afterwards. "
                f"({result.get('detail', '')})"
            )
    else:
        next_step = ("The compiled EA was installed. The chart reloads it by "
                     "itself, so there is nothing left to do.")

    return {"report": report, "needs_compile": needs_compile,
            "compiled": compiled, "next_step": next_step}


@router.post("/circuit-breaker/reset")
async def reset_circuit_breaker() -> dict:
    """Clear a tripped breaker.

    Money-adjacent: it is what lets automated entries resume after a losing
    streak. It places nothing itself, and the breaker trips again on its own
    terms if the streak continues.
    """
    settings_ctl.reset_circuit_breaker()
    return await settings_ctl.get_circuit_breaker_state_async()
