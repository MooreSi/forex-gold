"""Bridge watchdog check -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine._bridge_watchdog_loop's single-cycle
health check, as part of the core/engine.py migration series. See
docs/todo/refactor/core-bridge-watchdog-migration/020-*.md.

No MT5 order is ever placed, closed, or modified -- this function only
reads bridge health and toggles AutoTrading / restarts the bridge process.

`state` (a dict with `last_restart_at`/`was_connected`/`consecutive_fails`)
is taken as an explicit parameter and mutated in place -- instance state
that isn't derivable from the database, same pattern as
`retry_after`/`missing_streak`/`last_alert` in earlier packs, just three
related fields bundled in one dict. Callers should seed it with
`{"last_restart_at": 0.0, "was_connected": True, "consecutive_fails": 0}`,
matching the original loop's local-variable initial values.

Returns the number of seconds the caller should sleep before the next
check -- CHECK_INTERVAL (60) normally, or STARTUP_WAIT (20) right after a
successful restart launch, exactly matching the original's two different
post-check sleep durations. `start_bridge_process` is taken as an injected
async callable, same convention as `core-bot-commands-infra-migration` for
the same still-unextracted dependency.
"""
from __future__ import annotations

import asyncio
import logging
import time
# Imported by name so tests can pin it without patching the global time
# module (which the event loop also reads). The restart cooldown compares
# against this, so a test that used the real clock only passed on a machine
# that had been up longer than RESTART_COOLDOWN -- an intermittent CI
# failure that depended on container age, not on this code.
from time import monotonic
from typing import Any, Callable, Awaitable, Optional

from backend.src.services.telegram import alerts as telegram_alerts

log = logging.getLogger(__name__)

CHECK_INTERVAL = 60    # seconds between health checks
RESTART_COOLDOWN = 180  # minimum seconds between restart attempts
STARTUP_WAIT = 20      # seconds to wait after launching before next check
CONSECUTIVE_FAIL_THRESHOLD = 2


async def bridge_watchdog_check(
    bridge: Any,
    state: dict,
    bridge_inhibit_reconnect: bool,
    start_bridge_process: Callable[[], Awaitable[bool]],
    now_monotonic: Optional[float] = None,
) -> float:
    try:
        health = await bridge.get_health()
        connected = health.get("connected", False) or health.get("status") == "connected"
    except Exception:
        connected = False

    if connected:
        state["consecutive_fails"] = 0
        if not state["was_connected"]:
            log.info("Bridge watchdog: bridge is back online")
            state["was_connected"] = True
            # MT5 resets AutoTrading on reconnect after a maintenance window —
            # re-enable it automatically, same as the manual /restart_bridge command.
            try:
                at = await bridge.enable_autotrading()
                if at.get("enabled") and at.get("method") != "already_enabled":
                    at_msg = "Algo Trading re-enabled automatically."
                    log.info("Bridge watchdog: %s", at_msg)
                elif at.get("enabled"):
                    at_msg = "Algo Trading was already active."
                else:
                    at_msg = (
                        "Algo Trading is DISABLED — re-enable the AutoTrading "
                        "button in MT5 manually."
                    )
                    log.warning("Bridge watchdog: AutoTrading re-enable failed: %s",
                                at.get("error", "unknown"))
            except Exception as _at_err:
                at_msg = f"AutoTrading check failed: {_at_err}"
                log.warning("Bridge watchdog: %s", at_msg)
            asyncio.create_task(
                telegram_alerts.send_message(
                    f"MT5 bridge reconnected and healthy. {at_msg}"
                )
            )
        return CHECK_INTERVAL

    state["consecutive_fails"] += 1
    if state["consecutive_fails"] < CONSECUTIVE_FAIL_THRESHOLD:
        log.info(
            "Bridge watchdog: health check failed (%d/%d) — "
            "not yet treating as a real outage",
            state["consecutive_fails"], CONSECUTIVE_FAIL_THRESHOLD,
        )
        return CHECK_INTERVAL

    if state["was_connected"]:
        log.warning("Bridge watchdog: bridge offline (%d consecutive failed checks) "
                    "— auto-reconnect %s", state["consecutive_fails"],
                    "inhibited" if bridge_inhibit_reconnect else "will attempt")
        state["was_connected"] = False
        if not bridge_inhibit_reconnect:
            asyncio.create_task(
                telegram_alerts.send_message(
                    "MT5 bridge offline. Attempting automatic reconnect."
                )
            )

    if not bridge_inhibit_reconnect:
        now = now_monotonic if now_monotonic is not None else monotonic()
        if now - state["last_restart_at"] >= RESTART_COOLDOWN:
            log.info("Bridge watchdog: restarting bridge process")
            state["last_restart_at"] = now
            launched = await start_bridge_process()
            if launched:
                return STARTUP_WAIT

    return CHECK_INTERVAL
