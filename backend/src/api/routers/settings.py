"""Settings tab — twelve domains behind one tab, and the money ones are named.

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

import logging

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import settings_controller as settings_ctl

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/settings", tags=["settings"])

# Fields that must never leave the machine in a response, whatever shape the
# service hands back. A denylist rather than an allowlist because the services
# grow fields and a new secret must be excluded by default — the failure this
# guards is a password reaching a browser, which cannot be taken back.
SECRET_FIELDS = ("password", "api_key", "api_hash", "token", "secret", "passphrase")


class ConfigWrite(BaseModel):
    model_config = {"extra": "allow"}


class RetentionWrite(BaseModel):
    days: int


class ExpertParamsWrite(BaseModel):
    values: dict


class Mt5Credentials(BaseModel):
    login: str
    password: str
    server: str


def _redacted(values: dict) -> dict:
    """Everything except the secrets, with a flag saying one is set.

    `"password": ""` and `"password": null` both read as "no password
    configured", which is why the flag is separate from the value.
    """
    out = {}
    for key, value in (values or {}).items():
        if any(s in key.lower() for s in SECRET_FIELDS):
            out[f"{key}_set"] = bool(value)
        else:
            out[key] = value
    return out


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


@router.get("/email")
async def email() -> dict:
    return _redacted(settings_ctl.get_email_config())


@router.put("/email")
async def save_email(body: ConfigWrite) -> dict:
    settings_ctl.save_email_config(dict(body.model_dump()))
    return _redacted(settings_ctl.get_email_config())


@router.get("/telegram")
async def telegram() -> dict:
    return _redacted(settings_ctl.get_telegram_config())


@router.put("/telegram")
async def save_telegram(body: ConfigWrite) -> dict:
    settings_ctl.save_telegram_config(dict(body.model_dump()))
    return _redacted(settings_ctl.get_telegram_config())


@router.get("/mt5")
async def mt5() -> dict:
    """Which account is configured. **Never the password.**"""
    return _redacted(settings_ctl.get_mt5_credentials() or {})


@router.put("/mt5")
async def save_mt5(body: Mt5Credentials) -> dict:
    """Store MT5 credentials and push them to the bridge's own file.

    Both, always: credentials saved here and not synced leave the bridge
    authenticating as the previous account, which is the same shape of bug as
    backing up the wrong database — it looks like it worked.
    """
    settings_ctl.save_mt5_credentials(body.login, body.password, body.server)
    settings_ctl.sync_bridge_credentials_file()
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


@router.get("/diagnostics")
async def diagnostics() -> dict:
    """What the Diagnostics panel shows: the log since this app started, and
    the circuit breaker."""
    return {
        "log": [list(line) for line in await settings_ctl.live_log_lines()],
        "circuit_breaker": await settings_ctl.get_circuit_breaker_state_async(),
    }


@router.post("/circuit-breaker/reset")
async def reset_circuit_breaker() -> dict:
    """Clear a tripped breaker.

    Money-adjacent: it is what lets automated entries resume after a losing
    streak. It places nothing itself, and the breaker trips again on its own
    terms if the streak continues.
    """
    settings_ctl.reset_circuit_breaker()
    return await settings_ctl.get_circuit_breaker_state_async()
