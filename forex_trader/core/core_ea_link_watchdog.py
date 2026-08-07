"""EA link watchdog -- notices that the MQL5 EA has stopped talking to this
process and keeps the door open for it to come back.

WHY THIS EXISTS (2026-08-07)
---------------------------
MetaTrader restarted after a crash, reloaded the EA onto its chart (the
terminal log says "expert ForexTraderBridge loaded successfully"), and the EA
retried its socket every ~2.3s for the rest of the session -- at port 9101,
because a chart restored from a file written days earlier still carried that
InpPort. This process was listening on 9111. Nothing was wrong with either
side on its own, so neither log said anything: the app never saw a connection
attempt to report, and the EA's own log printed "connected" every retry
because SocketConnect under Wine claims success before the peer refuses.

The result was an EA badge quietly red for four hours with every trade
falling back to Python-side management and no alert anywhere.

So this loop watches the one fact that actually matters -- has the EA sent us
anything recently -- and, while it hasn't:
  * re-binds any listening port that wasn't available at startup, so a
    fallback port freed later is picked up without an app restart, and
  * says so once, in the log and on Telegram, naming the ports we are
    listening on so the mismatch is diagnosable from the alert alone.

It cannot re-attach the EA to a chart -- MetaTrader exposes no such control to
an outside process, and MT5 already restores the EA itself on restart. What it
can do is guarantee the app is reachable on every port the EA might dial, and
that a link which stays down is never silent.

No MT5 order is ever placed, closed, or modified here.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from forex_trader.core import telegram_alerts

log = logging.getLogger(__name__)

CHECK_INTERVAL = 15.0
# How long the link may be down before it's worth telling anyone. A terminal
# restart takes MT5 roughly a minute to cold-start, log into the broker and
# load the expert (observed 13:35:59 -> 13:36:09 for login, expert at
# 13:36:05); a healthy EA reconnects within ~2s of Python coming back. 90s is
# comfortably past the first and nowhere near the second, so a restart that
# heals itself never raises an alert.
DOWN_ALERT_AFTER_S = 90.0
# Repeat the alert at most this often while the link stays down -- the point
# is that it isn't silent, not that it's noisy.
REALERT_INTERVAL_S = 1800.0


def new_state() -> dict:
    return {"was_healthy": True, "down_since": 0.0, "last_alert_at": 0.0}


async def ea_link_check(
    bridge: Any,
    state: dict,
    now: Optional[float] = None,
    alert=None,
) -> float:
    """One cycle. Returns the seconds the caller should sleep afterwards.

    `bridge` is the EABridge instance; `state` is mutated in place (seed it
    with new_state()). `alert` defaults to telegram_alerts.send_message and is
    injectable so tests don't need Telegram config.
    """
    now = time.time() if now is None else now
    send = telegram_alerts.send_message if alert is None else alert

    healthy = False
    try:
        healthy = bool(bridge.is_ea_healthy())
    except Exception as e:
        log.debug("[EALink] health check raised: %s", e)

    if healthy:
        if not state["was_healthy"]:
            down_for = now - state["down_since"] if state["down_since"] else 0.0
            log.info("[EALink] EA reconnected after %.0fs offline", down_for)
            if state["last_alert_at"]:
                await send(
                    f"MT5 EA reconnected after {down_for / 60:.0f} min offline. "
                    "Native on-tick management is back."
                )
        state["was_healthy"] = True
        state["down_since"] = 0.0
        state["last_alert_at"] = 0.0
        return CHECK_INTERVAL

    if state["was_healthy"]:
        state["was_healthy"] = False
        state["down_since"] = now
        log.info("[EALink] EA link lost — waiting %.0fs for it to reconnect "
                 "on its own", DOWN_ALERT_AFTER_S)

    # Re-arm any port that couldn't be bound at startup. Cheap, and the only
    # active recovery available from this side.
    try:
        newly = await bridge.bind_ports()
        if newly:
            log.info("[EALink] now also listening on port(s) %s — an EA dialling "
                     "one of those can reach us again",
                     ", ".join(str(p) for p in newly))
    except Exception as e:
        log.debug("[EALink] re-bind failed: %s", e)

    down_for = now - state["down_since"]
    if down_for < DOWN_ALERT_AFTER_S:
        return CHECK_INTERVAL

    # An EA that has never connected in this process isn't a fault -- plenty
    # of installs never attach it. Only a link that existed and went away is
    # worth alerting on.
    if not getattr(bridge, "last_connected_at", 0.0):
        return CHECK_INTERVAL

    if state["last_alert_at"] and (now - state["last_alert_at"]) < REALERT_INTERVAL_S:
        return CHECK_INTERVAL

    try:
        ports = ", ".join(str(p) for p in bridge.listening_ports())
    except Exception:
        ports = "unknown"
    log.warning(
        "[EALink] EA has not been in contact for %.0fs. Listening on 127.0.0.1 "
        "port(s) %s. Check the EA is still on the chart with AutoTrading on, "
        "and that its InpPort matches one of those ports — a terminal restored "
        "after a crash can bring back an older InpPort.",
        down_for, ports,
    )
    state["last_alert_at"] = now
    await send(
        f"MT5 EA offline for {down_for / 60:.0f} min. Trades are falling back to "
        f"Python-side management. App is listening on port(s) {ports} — check "
        "the EA is attached with AutoTrading on and its InpPort matches."
    )
    return CHECK_INTERVAL
