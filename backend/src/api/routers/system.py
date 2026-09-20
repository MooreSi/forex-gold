"""App-level reads the shell needs: version, account badge, bridge health.

These are the header's data, and the header is on screen on every tab. They are
served as one consolidated payload rather than five endpoints because the React
conventions require one shared poll, and a shared poll wants one response:
"assembled server-side where it's one cheap query, not a fresh client
interval per field".
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from backend.src.api.deps import engine as engine_dep
from backend.src.controllers import broker_controller as broker_ctl
from backend.src.controllers import history_controller as history_ctl
from backend.src.controllers import settings_controller as settings_ctl
from backend.src.controllers import system_controller as system_ctl
from backend.src.controllers import sync_controller as sync_ctl
from backend.src.controllers import trading_controller as trading_ctl

router = APIRouter(prefix="/api/system", tags=["system"])


@router.get("/version")
async def version() -> dict:
    return {"version": system_ctl.app_version()}


@router.get("/releases")
async def releases() -> dict:
    """The changelog the About tab lists, newest first.

    Content, not state: it ships with the build and never changes while the app
    is running, so the dashboard fetches it once rather than polling it.
    """
    return {"version": system_ctl.app_version(), "releases": system_ctl.releases()}


@router.get("/header")
async def header(eng: Any = Depends(engine_dep)) -> dict:
    """Everything the header shows, in one read.

    `account` carries the demo/live distinction. The UI must make that
    unmistakable (frontend conventions §8), which it cannot do if the field is
    absent — so a bridge that cannot answer returns None here and the badge
    renders "unknown", never "demo".
    """
    account = await eng.get_mt5_account()
    health = await eng.get_bridge_health()
    tick = await eng.get_tick()
    # Equity minus net deposits: the only figure on screen that answers "am I
    # up or down overall". Cached five minutes behind the controller, because
    # this endpoint is polled every five seconds and the deposit total comes
    # from ten years of deal history.
    lifetime = await history_ctl.account_lifetime_pnl(
        eng, float((account or {}).get("equity") or 0.0),
    ) if account else None
    ea_ok, scope = broker_ctl.get_effective_ea_status()
    stale, stale_detail = broker_ctl.ea_build_status()
    colour, text, tooltip = broker_ctl.ea_badge_state(ea_ok, stale, scope, stale_detail)
    update_check = system_ctl.cached_update_check()
    return {
        "account": account,
        "bridge": health,
        "tick": tick.to_dict() if tick else None,
        "lifetime_pnl": lifetime,
        "active_trader": settings_ctl.get_active_trader(),
        # BOTH halts, not just the governor's. The circuit breaker writes a
        # different key, so a tripped breaker used to be invisible everywhere
        # except Settings > Diagnostics -- a header that says nothing while
        # automated entries are being refused.
        "pause": trading_ctl.trading_pause_status(),
        "remote_connected": sync_ctl.is_connected(),
        # The badge's colour and words are DECIDED by the service, not here and
        # not in the browser. A stale EA build with a green badge is the screen
        # contradicting the log, which is the bug `ea_badge_state` was extracted
        # for on 2026-09-09; re-deriving the rule in TypeScript would recreate
        # it in a second language.
        "ea_badge": {"colour": colour, "text": text, "tooltip": tooltip,
                     "stale": stale, "scope": scope},
        # The header's UPDATE AVAILABLE badge. From the CACHED check, never a
        # live one: `check_for_update` runs `git fetch`, and this endpoint is
        # polled every five seconds. Only the two facts the badge renders --
        # whether there is one and how many commits it is -- travel here; the
        # detail is fetched once, when the operator opens the popup.
        "update": {
            "available": update_check.get("available") is True,
            "commits": len(update_check.get("commits") or []),
            "remote_sha": str(update_check.get("remote_sha") or ""),
        },
    }
