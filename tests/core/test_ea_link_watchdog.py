"""core_ea_link_watchdog.ea_link_check -- the loop that notices the MQL5 EA
has stopped talking to this process.

The bug it exists for (2026-08-07): MT5 crashed, restarted, reloaded the EA,
and the EA then retried a port the app was not listening on for four hours.
Neither side logged anything, nothing alerted, and every trade silently fell
back to Python-side management.

No MT5 order is ever placed, closed, or modified by any of this.
"""
import asyncio
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
