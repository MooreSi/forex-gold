"""core_ea_link_watchdog.ea_link_check -- the loop that notices the MQL5 EA
has stopped talking to this process.

The bug it exists for (2026-08-07): MT5 crashed, restarted, reloaded the EA,
and the EA then retried a port the app was not listening on for four hours.
Neither side logged anything, nothing alerted, and every trade silently fell
back to Python-side management.

No MT5 order is ever placed, closed, or modified by any of this.
"""
import asyncio
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from forex_trader.core import core_ea_link_watchdog as wd


class FakeBridge:
    def __init__(self, healthy=True, last_connected_at=1000.0, ports=(9111,),
                 newly_bound=None):
        self.healthy = healthy
        self.last_connected_at = last_connected_at
        self._ports = list(ports)
        self._newly_bound = list(newly_bound or [])
        self.bind_calls = 0

    def is_ea_healthy(self):
        return self.healthy

    def listening_ports(self):
        return list(self._ports)

    async def bind_ports(self):
        self.bind_calls += 1
        newly, self._newly_bound = self._newly_bound, []
        self._ports.extend(newly)
        return newly


def _collector():
    sent = []

    async def _send(text):
        sent.append(text)
        return True

    return sent, _send


def test_healthy_link_alerts_nothing_and_keeps_state_clean():
    sent, send = _collector()
    bridge = FakeBridge(healthy=True)
    state = wd.new_state()

    sleep_for = asyncio.run(wd.ea_link_check(bridge, state, now=5000.0, alert=send))

    assert sleep_for == wd.CHECK_INTERVAL
    assert sent == []
    assert state["was_healthy"] is True
    assert state["down_since"] == 0.0
    # A healthy link must not touch the listeners at all.
    assert bridge.bind_calls == 0


def test_brief_outage_inside_the_grace_window_does_not_alert():
    """An MT5 restart takes about a minute to load the expert. That must not
    page anyone -- the EA reconnects on its own."""
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.DOWN_ALERT_AFTER_S - 1,
                                 alert=send))

    assert sent == []
    assert state["was_healthy"] is False
    assert state["down_since"] == 1000.0


def test_outage_past_the_grace_window_alerts_once_and_names_the_ports():
    sent, send = _collector()
    bridge = FakeBridge(healthy=False, ports=(9111, 9101))
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.DOWN_ALERT_AFTER_S + 1,
                                 alert=send))

    assert len(sent) == 1
    # The ports are the whole diagnosis -- a mismatch against the EA's InpPort
    # is invisible without them.
    assert "9111" in sent[0] and "9101" in sent[0]

    # Still down a minute later: no second alert until REALERT_INTERVAL_S.
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.DOWN_ALERT_AFTER_S + 61,
                                 alert=send))
    assert len(sent) == 1

    # ...and one after the re-alert interval.
    asyncio.run(wd.ea_link_check(
        bridge, state,
        now=1000.0 + wd.DOWN_ALERT_AFTER_S + wd.REALERT_INTERVAL_S + 2,
        alert=send))
    assert len(sent) == 2


def test_ea_that_never_connected_in_this_process_is_not_a_fault():
    """Plenty of installs never attach the EA. Silence, not a standing alert."""
    sent, send = _collector()
    bridge = FakeBridge(healthy=False, last_connected_at=0.0)
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.DOWN_ALERT_AFTER_S + 60,
                                 alert=send))

    assert sent == []


def test_recovery_after_an_alerted_outage_reports_the_reconnect():
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.DOWN_ALERT_AFTER_S + 1,
                                 alert=send))
    assert len(sent) == 1

    bridge.healthy = True
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + 600, alert=send))

    assert len(sent) == 2
    assert "reconnected" in sent[1]
    assert state["was_healthy"] is True
    assert state["down_since"] == 0.0
    assert state["last_alert_at"] == 0.0


def test_recovery_inside_the_grace_window_stays_silent():
    """Dropped and back before anyone was told: nothing to announce."""
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))
    bridge.healthy = True
    asyncio.run(wd.ea_link_check(bridge, state, now=1030.0, alert=send))

    assert sent == []
    assert state["was_healthy"] is True


def test_while_down_it_retries_binding_ports_it_could_not_claim_at_startup():
    """The one active recovery available: a fallback port held by another
    process at startup gets picked up once that process lets go, with no app
    restart."""
    sent, send = _collector()
    bridge = FakeBridge(healthy=False, ports=(9111,), newly_bound=[9101])
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))

    assert bridge.bind_calls == 1
    assert bridge.listening_ports() == [9111, 9101]


def test_a_bridge_whose_health_check_raises_counts_as_down_not_as_healthy():
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)
    bridge.is_ea_healthy = MagicMock(side_effect=RuntimeError("boom"))
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))

    assert state["was_healthy"] is False
    assert state["down_since"] == 1000.0


def test_a_failing_rebind_does_not_stop_the_alert():
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)

    async def _boom():
        raise OSError("address in use")

    bridge.bind_ports = _boom
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send))
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.DOWN_ALERT_AFTER_S + 1,
                                 alert=send))

    assert len(sent) == 1


# ── Terminal restart recovery ────────────────────────────────────────────────
# Nothing here can reattach the EA directly; restarting the terminal is the
# indirect route, since MT5 restores its own charts and experts on startup.
# Every test below is about a guard, because the failure mode of getting this
# wrong is an app that cycles MT5 for days while nobody is at the keyboard.

class FakeTick:
    def __init__(self, timestamp):
        self.timestamp = timestamp


class FakeMT5Bridge:
    """`ticking=False` is a shut market: the tick timestamp stops moving, which
    is the only signal that distinguishes a closed session from a dead EA."""

    def __init__(self, connected=True, ticking=True, tick_ts=500000.0,
                 serves_ticks=True):
        self.connected = connected
        self.ticking = ticking
        self.serves_ticks = serves_ticks
        self._tick_ts = tick_ts

    async def get_health(self):
        return {"connected": self.connected}

    async def get_tick(self):
        if not self.serves_ticks:
            return None
        if self.ticking:
            self._tick_ts += 1.0
        return FakeTick(self._tick_ts)


def _restarter(result=True, raises=None):
    calls = []

    async def _restart():
        calls.append(True)
        if raises is not None:
            raise raises
        return result

    return calls, _restart


def _run_down_for(bridge, state, seconds, send, **kw):
    """Take the link down at t=1000 and check again `seconds` later."""
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send, **kw))
    return asyncio.run(
        wd.ea_link_check(bridge, state, now=1000.0 + seconds, alert=send, **kw)
    )


def test_restart_fires_once_the_link_has_been_down_long_enough():
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_down_for(bridge, state, wd.RESTART_AFTER_S + 1, send,
                  mt5_bridge=FakeMT5Bridge(connected=True), restart_bridge=restart)

    assert calls == [True]
    assert state["restarts"] == 1
    assert any("Restarting MT5" in s for s in sent)


def test_no_restart_while_the_outage_could_still_heal_itself():
    """A terminal cold-start plus broker login plus expert load runs about
    70s. Restarting inside that window would interrupt the recovery it is
    supposed to be causing."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_down_for(bridge, state, wd.RESTART_AFTER_S - 1, send,
                  mt5_bridge=FakeMT5Bridge(connected=True), restart_bridge=restart)

    assert calls == []


def test_no_restart_when_the_mt5_bridge_is_down_too():
    """Then it is not an EA-only fault and core_bridge_watchdog owns the
    recovery. Both killing the same Wine session would interleave a restart
    with a startup."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_down_for(bridge, state, wd.RESTART_AFTER_S + 1, send,
                  mt5_bridge=FakeMT5Bridge(connected=False), restart_bridge=restart)

    assert calls == []
    assert state["restarts"] == 0


def test_no_restart_when_the_user_stopped_the_bridge_by_hand():
    """Relaunching MT5 underneath someone who explicitly stopped it is the one
    thing the inhibit flag exists to prevent."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_down_for(bridge, state, wd.RESTART_AFTER_S + 1, send,
                  mt5_bridge=FakeMT5Bridge(connected=True), restart_bridge=restart,
                  inhibit_reconnect=True)

    assert calls == []


def test_no_restart_for_an_ea_that_never_connected():
    """An install that simply doesn't use the EA must never have its terminal
    bounced on that basis."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False, last_connected_at=0.0)
    state = wd.new_state()

    _run_down_for(bridge, state, wd.RESTART_AFTER_S + 1, send,
                  mt5_bridge=FakeMT5Bridge(connected=True), restart_bridge=restart)

    assert calls == []


def test_cooldown_holds_off_the_second_attempt():
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()
    kw = {"mt5_bridge": FakeMT5Bridge(connected=True), "restart_bridge": restart}

    _run_down_for(bridge, state, wd.RESTART_AFTER_S + 1, send, **kw)
    assert calls == [True]

    # MT5 needs its two minutes back before the link can even be judged.
    asyncio.run(wd.ea_link_check(
        bridge, state, now=1000.0 + wd.RESTART_AFTER_S + wd.RESTART_COOLDOWN_S - 1,
        alert=send, **kw))
    assert len(calls) == 1

    asyncio.run(wd.ea_link_check(
        bridge, state, now=1000.0 + wd.RESTART_AFTER_S + wd.RESTART_COOLDOWN_S + 2,
        alert=send, **kw))
    assert len(calls) == 2


def test_restarts_stop_at_the_cap_and_say_so():
    """An EA removed from the chart, or one that failed to compile, will never
    come back however many times MT5 is bounced. Past the cap it is a human's
    problem, not a restart loop's."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()
    kw = {"mt5_bridge": FakeMT5Bridge(connected=True), "restart_bridge": restart}

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send, **kw))
    t = 1000.0 + wd.RESTART_AFTER_S + 1
    for _ in range(wd.MAX_RESTARTS + 3):
        asyncio.run(wd.ea_link_check(bridge, state, now=t, alert=send, **kw))
        t += wd.RESTART_COOLDOWN_S + 1

    assert len(calls) == wd.MAX_RESTARTS
    assert any("No more restarts will be attempted" in s for s in sent)


def test_reconnecting_gives_the_next_outage_a_full_restart_budget():
    """MAX_RESTARTS counts consecutive failures, not restarts for all time."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()
    kw = {"mt5_bridge": FakeMT5Bridge(connected=True), "restart_bridge": restart}

    _run_down_for(bridge, state, wd.RESTART_AFTER_S + 1, send, **kw)
    assert state["restarts"] == 1

    bridge.healthy = True
    asyncio.run(wd.ea_link_check(bridge, state, now=2000.0, alert=send, **kw))

    assert state["restarts"] == 0


def test_restart_is_attempted_even_when_the_alert_is_throttled():
    """The re-alert throttle is about noise. A cycle too soon to alert again
    must still be able to act -- getting this order wrong would have made the
    recovery unreachable for the first 30 minutes of every outage."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()
    kw = {"mt5_bridge": FakeMT5Bridge(connected=True), "restart_bridge": restart}

    # First alert lands at 90s; the restart is not due until 600s, well inside
    # the 1800s re-alert throttle.
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send, **kw))
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.DOWN_ALERT_AFTER_S + 1,
                                 alert=send, **kw))
    assert calls == []
    assert state["last_alert_at"] > 0

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0 + wd.RESTART_AFTER_S + 1,
                                 alert=send, **kw))

    assert calls == [True]


def test_a_failing_restart_does_not_escape_the_loop():
    sent, send = _collector()
    calls, restart = _restarter(raises=RuntimeError("wineserver would not die"))
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    sleep_for = _run_down_for(
        bridge, state, wd.RESTART_AFTER_S + 1, send,
        mt5_bridge=FakeMT5Bridge(connected=True), restart_bridge=restart)

    assert sleep_for == wd.CHECK_INTERVAL
    assert any("restart failed" in s for s in sent)


def test_without_a_restarter_it_stays_alert_only():
    """The default. Every caller that isn't the engine gets the old behaviour,
    and a build with no recovery wired in still reports the outage."""
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_down_for(bridge, state, wd.RESTART_AFTER_S + 1, send)

    assert state["restarts"] == 0
    assert len(sent) == 1


# ── Closed markets are not outages ───────────────────────────────────────────
# The EA's heartbeat runs on TimeCurrent(), which in MQL5 is the time of the
# last quote, not a clock. No ticks means no advancing TimeCurrent(), so the
# pings simply stop -- every weekend, and in the daily gap between the New York
# close and the Asian open. The EA is fine; alerting on it is noise, and
# bouncing MT5 for it is worse than noise.

_SATURDAY = datetime(2026, 8, 8, 12, 0, tzinfo=timezone.utc).timestamp()


def _run_at(bridge, state, start, seconds, send, **kw):
    asyncio.run(wd.ea_link_check(bridge, state, now=start, alert=send, **kw))
    return asyncio.run(
        wd.ea_link_check(bridge, state, now=start + seconds, alert=send, **kw)
    )


@pytest.mark.live_market_hours
def test_weekend_does_not_alert():
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_at(bridge, state, _SATURDAY, wd.DOWN_ALERT_AFTER_S + 1, send,
            mt5_bridge=FakeMT5Bridge(connected=True))

    assert sent == []


@pytest.mark.live_market_hours
def test_weekend_does_not_restart_mt5():
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_at(bridge, state, _SATURDAY, wd.RESTART_AFTER_S + 1, send,
            mt5_bridge=FakeMT5Bridge(connected=True), restart_bridge=restart)

    assert calls == []
    assert sent == []


def test_a_market_that_stopped_ticking_is_not_an_ea_fault():
    """The daily NY-close-to-Asian-open break, holidays, and broker
    maintenance -- none of which any hardcoded schedule would know about, and
    all of which stop the EA's heartbeat exactly the way a weekend does."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()
    mt5 = FakeMT5Bridge(connected=True, ticking=False)
    kw = {"mt5_bridge": mt5, "restart_bridge": restart}

    # First cycle records the tick; from then on it never moves.
    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send, **kw))
    t = 1000.0
    for _ in range(12):
        t += wd.TICK_STALE_S
        asyncio.run(wd.ea_link_check(bridge, state, now=t, alert=send, **kw))

    assert sent == []
    assert calls == []


def test_a_live_market_still_alerts_and_restarts():
    """The gate must not swallow the real thing it was built for."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_at(bridge, state, 1000.0, wd.RESTART_AFTER_S + 1, send,
            mt5_bridge=FakeMT5Bridge(connected=True, ticking=True),
            restart_bridge=restart)

    assert calls == [True]
    assert len(sent) >= 1


def test_reopening_gives_the_ea_a_fresh_grace_period():
    """The whole point of resetting the clock rather than pausing it. A link
    quiet all weekend must not reopen into a 49-hour "outage" and get MT5
    restarted seconds before the EA would have reconnected on its own."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()
    mt5 = FakeMT5Bridge(connected=True, ticking=False)
    kw = {"mt5_bridge": mt5, "restart_bridge": restart}

    asyncio.run(wd.ea_link_check(bridge, state, now=1000.0, alert=send, **kw))
    t = 1000.0
    for _ in range(20):                      # a long shut market
        t += wd.RESTART_AFTER_S
        asyncio.run(wd.ea_link_check(bridge, state, now=t, alert=send, **kw))
    assert calls == []

    # Quotes resume. The EA is still quiet, but it has only just had the
    # chance to reconnect, so nothing fires yet.
    mt5.ticking = True
    t += wd.CHECK_INTERVAL
    asyncio.run(wd.ea_link_check(bridge, state, now=t, alert=send, **kw))
    assert calls == []
    assert sent == []

    # Still quiet a full restart window into the live session: now it is real.
    t += wd.RESTART_AFTER_S + 1
    asyncio.run(wd.ea_link_check(bridge, state, now=t, alert=send, **kw))
    assert calls == [True]


def test_bridge_not_serving_ticks_is_not_treated_as_an_ea_fault():
    """No ticks to judge by means no evidence the EA failed -- and a bridge
    that cannot answer is already the bridge watchdog's alert to raise."""
    sent, send = _collector()
    calls, restart = _restarter()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_at(bridge, state, 1000.0, wd.RESTART_AFTER_S + 1, send,
            mt5_bridge=FakeMT5Bridge(connected=True, serves_ticks=False),
            restart_bridge=restart)

    assert calls == []
    assert sent == []


def test_without_an_mt5_bridge_liveness_is_not_guessed():
    """Nothing to judge with, so the watchdog behaves as it did before the
    gate existed rather than suppressing on an assumption."""
    sent, send = _collector()
    bridge = FakeBridge(healthy=False)
    state = wd.new_state()

    _run_at(bridge, state, 1000.0, wd.DOWN_ALERT_AFTER_S + 1, send)

    assert len(sent) == 1
