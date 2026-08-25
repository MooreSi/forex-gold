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
    fallback port freed later is picked up without an app restart,
  * says so once, in the log and on Telegram, naming the ports we are
    listening on so the mismatch is diagnosable from the alert alone, and
  * after RESTART_AFTER_S, restarts the terminal so MT5 reloads the EA.

Nothing here can re-attach the EA to a chart directly -- MetaTrader exposes no
such control to an outside process. Restarting the terminal is the indirect
route: MT5 restores its own charts, and with them the expert, on startup. That
is the same recovery core_bridge_watchdog already performs, wired to the other
failure -- and the gap between them is exactly the 2026-08-07 outage, where
the *bridge* stayed healthy (prices, account and manual orders all fine) so
its watchdog correctly did nothing, while the EA sat dead for four hours.

WHY RESTARTING IS THE SAFE CHOICE HERE, NOT THE RISKY ONE
---------------------------------------------------------
It sounds worse than it is. Positions and pending orders live on the broker's
server, not in the terminal; killing terminal64.exe interrupts *management*,
it does not cancel or close anything. And by the time this fires the EA has
already been dead for RESTART_AFTER_S, so EA-managed trades are unmanaged
either way -- for template strategies especially, which core_monitor_loop
deliberately refuses to reclaim into Python (there is no Python handler for a
"template:<name>" strategy, and the one time one was reclaimed it fabricated a
$40,730 profit against a placeholder entry price). A trade mid-grid is
therefore a reason to restart sooner, not a reason to hold off.

What it does cost is roughly two minutes where the MT5 bridge is down too, so
Python-side management is blind as well. That is the whole trade being made:
two minutes of nothing against an outage that otherwise lasts until someone is
at the keyboard. Hence RESTART_AFTER_S is generous, the cooldown is long, and
MAX_RESTARTS caps it -- an EA that is genuinely broken (removed from the
chart, failed to compile, AutoTrading off) must not put the terminal into a
restart cycle for the rest of the week. Past the cap it goes back to alerting
and waits for a human.

A CLOSED MARKET IS NOT AN OUTAGE
--------------------------------
The EA looks dead every weekend and in the daily gap between the New York
close and the Asian open, and it is not: its heartbeat is driven by
TimeCurrent(), which in MQL5 is the time of the *last quote received*, not a
clock. No ticks, no advancing TimeCurrent(), so PollSocket's
"TimeCurrent() - g_lastPingSent >= 2" never comes true and the pings simply
stop. The socket stays open, the EA is fine, and is_ea_healthy() goes False
anyway because nothing has arrived within its 8s window.

So everything below is gated on the market actually ticking, and while it
isn't, the outage clock is reset rather than merely paused -- when the market
reopens the EA gets the full grace period to come back before anything fires,
instead of reopening into a 49-hour "outage" and being restarted seconds
before it would have reconnected on its own.

Liveness is judged by watching the MT5 bridge's tick timestamp *move*, not by
comparing it to the local clock: MT5 reports tick.time in broker-server time,
which sits a couple of hours off true UTC, so an absolute freshness test would
be wrong by that offset in whichever direction the broker's timezone happens
to fall. Movement is immune to that, and it covers holidays and broker
maintenance windows too, neither of which any hardcoded schedule would know
about. The weekend is additionally checked against dpm_engine's fixed
Fri-21:00/Sun-22:00 UTC window, which needs no bridge at all and so still
answers when the bridge is the thing that is down.

WHERE THE RECOVERY IS AVAILABLE
-------------------------------
Only where restarting actually reloads the expert, which means only the
macOS/Wine bridge: engine._start_bridge_process tears down the whole Wine
session there, terminal64.exe included, so MT5 cold-starts and restores its
charts. Its Windows path restarts mt5_bridge.py alone and the native bridge
merely reconnects in-process -- the terminal keeps running on both, the EA is
never reloaded, and a restart would drop the bridge for nothing. The engine
withholds `restart_bridge` in those cases and this degrades to alert-only, so
a VPS running native MT5 still reports the outage but will not bounce itself.

No MT5 order is ever placed, closed, or modified here.
"""
from __future__ import annotations

import logging
import time
from typing import Any, Optional

from backend.src.services.telegram import alerts
from backend.src.services.telegram import alerts as telegram_alerts

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

# How long the link must stay down before restarting the terminal to make MT5
# reload the EA. Well past every self-healing case: a terminal cold-start plus
# broker login plus expert load ran ~70s on 2026-08-07, and a healthy EA is
# back within ~2s of Python returning. Ten minutes means nothing that would
# have recovered on its own is ever interrupted.
RESTART_AFTER_S = 600.0
# Minimum gap between restart attempts. Long, because a restart is only worth
# repeating if the first one truly failed, and MT5 needs its ~2 minutes to
# come back before the link can even be judged.
RESTART_COOLDOWN_S = 1800.0
# Consecutive restarts before giving up and leaving it to a human. An EA
# removed from the chart, failed to compile, or blocked by AutoTrading being
# off will never come back no matter how many times MT5 is bounced.
MAX_RESTARTS = 3

# How long the bridge's tick timestamp may sit unchanged before the market is
# treated as shut. XAUUSD ticks many times a second in every live session, so
# even the quietest stretch of the Asian session is orders of magnitude below
# this; the daily break is around an hour and the weekend around 49.
#
# Must stay BELOW DOWN_ALERT_AFTER_S. Liveness is judged by movement, so the
# gate needs this much history before it can call a market shut -- and if that
# took longer than the alert threshold, an app started during a closed market
# would alert once before the gate could stop it. Erring short is the safe
# direction anyway: a false "closed" only suppresses alerting until quotes
# resume, whereas a false "open" is the weekend noise this exists to end.
TICK_STALE_S = 60.0


def new_state() -> dict:
    return {"was_healthy": True, "down_since": 0.0, "last_alert_at": 0.0,
            "last_restart_at": 0.0, "restarts": 0,
            "last_tick_ts": 0.0, "last_tick_change_at": 0.0,
            "market_closed": False}


async def ea_link_check(
    bridge: Any,
    state: dict,
    now: Optional[float] = None,
    alert=None,
    mt5_bridge: Any = None,
    restart_bridge=None,
    inhibit_reconnect: bool = False,
) -> float:
    """One cycle. Returns the seconds the caller should sleep afterwards.

    `bridge` is the EABridge instance; `state` is mutated in place (seed it
    with new_state()). `alert` defaults to telegram_alerts.send_message and is
    injectable so tests don't need Telegram config.

    `restart_bridge` is engine._start_bridge_process -- the same terminal
    relaunch core_bridge_watchdog uses. Omit it (the default) and this
    degrades to alert-only, which is what every test that isn't about the
    restart wants. `mt5_bridge` is the MT5 bridge, checked so the two
    watchdogs never both bounce the same Wine session; `inhibit_reconnect`
    is the engine's flag for a bridge the user stopped by hand.
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
        # Only *consecutive* restarts count against MAX_RESTARTS. A link that
        # came back has earned the full budget again for the next outage.
        state["restarts"] = 0
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

    # An EA that has never connected in this process isn't a fault -- plenty
    # of installs never attach it. Only a link that existed and went away is
    # worth watching, alerting on, or restarting the terminal for.
    if not getattr(bridge, "last_connected_at", 0.0):
        return CHECK_INTERVAL

    # Sampled from the very first quiet cycle, not lazily when a decision is
    # due: liveness is judged by the tick timestamp *moving*, so the first
    # sample can only ever read "live" -- there is nothing yet to compare it
    # against. Deferring this until the alert was due meant a market that shut
    # before the app started got one free alert before the gate had any
    # history. Starting here gives TICK_STALE_S of evidence to accumulate
    # inside the grace window below, which is why it must stay the shorter of
    # the two.
    live, why_not = await _market_is_live(state, now, mt5_bridge)
    if not live:
        # Reset rather than pause: the EA stops pinging the moment quotes stop
        # (its heartbeat runs on TimeCurrent()), so this is the normal state of
        # every weekend and every daily break. Restarting the clock means the
        # reopen gets a full grace period instead of inheriting hours of
        # "downtime" that was only ever the market being shut.
        state["down_since"] = now
        if not state["market_closed"]:
            state["market_closed"] = True
            log.info("[EALink] EA quiet but the market is not ticking (%s) — "
                     "expected, not alerting or restarting", why_not)
        return CHECK_INTERVAL

    if state["market_closed"]:
        state["market_closed"] = False
        log.info("[EALink] market is ticking again — EA link is back under watch")

    down_for = now - state["down_since"]
    if down_for < DOWN_ALERT_AFTER_S:
        return CHECK_INTERVAL

    # Before the re-alert throttle below, not after: a cycle that is merely too
    # soon to alert again must still be able to attempt the recovery.
    await _maybe_restart_terminal(
        state, now, down_for, send, mt5_bridge, restart_bridge, inhibit_reconnect,
    )

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


async def _market_is_live(state: dict, now: float, mt5_bridge: Any) -> tuple[bool, str]:
    """(is_ticking, reason_if_not). See the module docstring: the EA stops
    heartbeating whenever quotes stop, so "no ticks" fully explains its
    silence and must never be reported as a fault."""
    from datetime import datetime, timezone
    # Local import, same as core_db_risk_settings does, to avoid a cycle.
    from backend.src.services.dpm.engine import is_weekly_market_closed

    # Needs no bridge, so it still answers when the bridge is the casualty.
    if is_weekly_market_closed(datetime.fromtimestamp(now, timezone.utc)):
        return False, "weekend"

    if mt5_bridge is None:
        return True, ""   # nothing to judge with; don't suppress on a guess

    try:
        tick = await mt5_bridge.get_tick()
    except Exception as e:
        log.debug("[EALink] tick fetch raised: %s", e)
        tick = None
    if tick is None:
        # The bridge itself is not answering. That is the bridge watchdog's
        # problem, and it is not evidence the EA has failed.
        return False, "MT5 bridge is not serving ticks"

    ts = float(getattr(tick, "timestamp", 0.0) or 0.0)
    if ts <= 0:
        return True, ""   # no usable timestamp; fall back to old behaviour

    if ts != state["last_tick_ts"]:
        state["last_tick_ts"] = ts
        state["last_tick_change_at"] = now
        return True, ""

    if not state["last_tick_change_at"]:
        state["last_tick_change_at"] = now
        return True, ""

    quiet_for = now - state["last_tick_change_at"]
    if quiet_for >= TICK_STALE_S:
        return False, f"no new tick for {quiet_for / 60:.0f} min"
    return True, ""


async def _maybe_restart_terminal(
    state: dict,
    now: float,
    down_for: float,
    send,
    mt5_bridge: Any,
    restart_bridge,
    inhibit_reconnect: bool,
) -> bool:
    """Restart the terminal so MT5 reloads the EA, if every guard allows it.

    Returns True if a restart was launched. Each guard below is a way this
    could do more harm than the outage it is fixing, so they are all checked
    before anything is killed.
    """
    if restart_bridge is None:
        return False   # caller opted out (tests, or a build without recovery)

    if inhibit_reconnect:
        # The user stopped the bridge by hand. Relaunching MT5 underneath them
        # is the one thing they have explicitly said not to do.
        log.info("[EALink] EA down %.0fs but bridge auto-reconnect is inhibited "
                 "(manual stop) — not restarting", down_for)
        return False

    if down_for < RESTART_AFTER_S:
        return False

    if state["restarts"] >= MAX_RESTARTS:
        return False   # already said so once, at the point the cap was hit

    if state["last_restart_at"] and (now - state["last_restart_at"]) < RESTART_COOLDOWN_S:
        return False

    # If the MT5 bridge is down too, this is not an EA-only fault and
    # core_bridge_watchdog already owns the recovery. Two watchdogs killing the
    # same Wine session would interleave restart with startup and could leave
    # the terminal down for longer than either alone.
    if mt5_bridge is not None:
        try:
            health = await mt5_bridge.get_health()
            connected = bool(health.get("connected") or health.get("status") == "connected")
        except Exception as e:
            log.debug("[EALink] MT5 bridge health check raised: %s", e)
            connected = False
        if not connected:
            log.info("[EALink] EA down %.0fs but the MT5 bridge is down too — "
                     "leaving the restart to the bridge watchdog", down_for)
            return False

    attempt = state["restarts"] + 1
    log.warning(
        "[EALink] EA offline %.0fs with a healthy MT5 bridge — restarting the "
        "terminal so MT5 reloads the expert (attempt %d/%d)",
        down_for, attempt, MAX_RESTARTS,
    )
    await send(
        f"MT5 EA has been offline {down_for / 60:.0f} min. Restarting MT5 so it "
        f"reloads the EA (attempt {attempt} of {MAX_RESTARTS}). Open positions and "
        "pending orders are held by the broker and are not affected; management "
        "is blind for about two minutes while the terminal comes back."
    )

    state["last_restart_at"] = now
    state["restarts"] = attempt
    try:
        launched = await restart_bridge()
    except Exception as e:
        log.warning("[EALink] terminal restart failed: %s", e)
        await send(f"MT5 restart failed: {e}. The EA is still offline.")
        return False

    if not launched:
        log.warning("[EALink] terminal restart did not launch")
        return False

    if state["restarts"] >= MAX_RESTARTS:
        log.warning("[EALink] that was restart %d of %d — no further automatic "
                    "restarts until the EA reconnects", state["restarts"], MAX_RESTARTS)
        await send(
            f"That was automatic restart {MAX_RESTARTS} of {MAX_RESTARTS}. If the EA "
            "is still offline after this one it needs a look: check it is on the "
            "chart, compiled, and that AutoTrading is on. No more restarts will be "
            "attempted until it reconnects."
        )
    return True
