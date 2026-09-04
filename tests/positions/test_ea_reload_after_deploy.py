"""Loading a freshly deployed EA without anyone at the terminal.

`ea_deploy` puts the new file in the terminal's Experts folder; nothing there
makes MetaTrader RUN it. Attaching an EA to a chart has no runtime API at all
-- the only lever from outside is restarting the terminal, which makes MT5
restore its charts and reload the expert with them. That lever already exists
and is already trusted: `core_ea_link_watchdog` pulls it when the EA has been
silent for ten minutes.

The owner's call (2026-09-04): "if mt5 needs to reload to reattach the newly
compiled ea that is fine, you can build this into the code."

So this fires on a DIFFERENT fact from the outage restart. There the EA is
dead and a restart costs nothing that is not already lost. Here the EA is
alive, healthy, and managing trades -- it is merely the wrong version -- and a
restart blinds management for the ~2 minutes MT5 takes to come back. That is
cheap when nothing is at risk and expensive when something is, which is why
the gate is "no trade slots in use at all": no open position, no order resting
at the broker, no open in flight.

Four ways it declines, one way it goes. Nothing here restarts anything: the
restarter is a sentinel that records being called.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.broker import ea_deploy


# ── The decision ──────────────────────────────────────────────────────────────

class TestWhenToReload:

    def test_a_stale_ea_with_an_idle_book_reloads(self):
        d = ea_deploy.reload_decision(ea_version_ok=False, slots_in_use=0,
                                      can_reload=True, already_tried=False)

        assert d["reload"] is True

    def test_a_current_ea_is_left_alone(self):
        """The common case by far. Restarting a terminal that is already
        running the right build is pure cost."""
        d = ea_deploy.reload_decision(ea_version_ok=True, slots_in_use=0,
                                      can_reload=True, already_tried=False)

        assert d["reload"] is False

    def test_an_unknown_version_is_not_treated_as_stale(self):
        """None means there was nothing to compare against -- a packaged
        install ships the .ex5 without the .mq5. Unknown is not evidence."""
        d = ea_deploy.reload_decision(ea_version_ok=None, slots_in_use=0,
                                      can_reload=True, already_tried=False)

        assert d["reload"] is False

    def test_an_open_position_defers_it(self):
        d = ea_deploy.reload_decision(ea_version_ok=False, slots_in_use=1,
                                      can_reload=True, already_tried=False)

        assert d["reload"] is False

    def test_the_deferral_says_why(self):
        """This reaches the operator: a new EA sitting unloaded needs to be a
        known state, not a silent one."""
        d = ea_deploy.reload_decision(ea_version_ok=False, slots_in_use=2,
                                      can_reload=True, already_tried=False)

        assert "2" in d["reason"]

    def test_a_bridge_whose_restart_would_not_reload_the_ea_declines(self):
        """Only the macOS/Wine path tears the terminal down. On Windows the
        native bridge reconnects in-process, the terminal keeps running, and a
        restart would drop the bridge for nothing."""
        d = ea_deploy.reload_decision(ea_version_ok=False, slots_in_use=0,
                                      can_reload=False, already_tried=False)

        assert d["reload"] is False

    def test_it_only_tries_once(self):
        """The loop this prevents is real: on macOS nothing can compile, so a
        new .mq5 with no .ex5 beside it reloads the SAME old build, reports the
        same stale version, and would restart the terminal every cycle
        forever."""
        d = ea_deploy.reload_decision(ea_version_ok=False, slots_in_use=0,
                                      can_reload=True, already_tried=True)

        assert d["reload"] is False


# ── The watchdog branch ───────────────────────────────────────────────────────

from backend.src.services.positions import core_ea_link_watchdog as wd


class _EABridge:
    def __init__(self, version_ok=False):
        self.ea_version_ok = version_ok
        self.last_connected_at = 1.0

    def is_ea_healthy(self):
        return True

    def listening_ports(self):
        return [9111]


@pytest.fixture
def sent():
    return []


@pytest.fixture
def alert(sent):
    async def _send(text, *a, **k):
        sent.append(text)
    return _send


@pytest.fixture
def restarter():
    calls = []

    async def _restart():
        calls.append(True)
        return True
    _restart.calls = calls
    return _restart


@pytest.fixture
def idle(monkeypatch):
    """No open position, no resting order, nothing in flight."""
    monkeypatch.setattr(wd, "_slots_in_use", lambda: 0)


def _run(bridge, state, restarter, alert):
    return asyncio.run(wd.ea_link_check(
        bridge, state, now=1_000.0, alert=alert, restart_bridge=restarter))


def test_a_healthy_but_stale_ea_restarts_the_terminal(idle, restarter, alert):
    state = wd.new_state()

    _run(_EABridge(version_ok=False), state, restarter, alert)

    assert restarter.calls == [True]


def test_a_healthy_current_ea_restarts_nothing(idle, restarter, alert):
    """Negative control. Without it every test here would pass against a
    watchdog that bounces the terminal on every cycle."""
    state = wd.new_state()

    _run(_EABridge(version_ok=True), state, restarter, alert)

    assert restarter.calls == []


def test_an_open_trade_holds_the_reload_back(monkeypatch, restarter, alert):
    monkeypatch.setattr(wd, "_slots_in_use", lambda: 1)
    state = wd.new_state()

    _run(_EABridge(version_ok=False), state, restarter, alert)

    assert restarter.calls == []


def test_the_operator_is_told_before_the_terminal_goes_down(idle, restarter,
                                                            alert, sent):
    state = wd.new_state()

    _run(_EABridge(version_ok=False), state, restarter, alert)

    assert sent and "restart" in sent[0].lower()


def test_it_does_not_restart_twice_for_the_same_stale_build(idle, restarter, alert):
    state = wd.new_state()

    _run(_EABridge(version_ok=False), state, restarter, alert)
    _run(_EABridge(version_ok=False), state, restarter, alert)

    assert restarter.calls == [True]


def test_no_restarter_means_no_attempt(idle, alert):
    """Alert-only builds (Windows, native bridge, and every test that isn't
    about restarting) must not blow up on the new branch."""
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(_EABridge(version_ok=False), state,
                                 now=1_000.0, alert=alert, restart_bridge=None))


def test_a_manual_bridge_stop_is_respected(idle, restarter, alert):
    """The user stopped the bridge by hand. Relaunching MT5 underneath them is
    the one thing they have explicitly said not to do."""
    state = wd.new_state()

    asyncio.run(wd.ea_link_check(_EABridge(version_ok=False), state, now=1_000.0,
                                 alert=alert, restart_bridge=restarter,
                                 inhibit_reconnect=True))

    assert restarter.calls == []


def test_a_slot_count_that_raises_holds_the_reload_back(monkeypatch, restarter, alert):
    """Unknown is not idle. If the book cannot be read, the safe reading is
    that something is open."""
    def _boom():
        raise RuntimeError("db gone")
    monkeypatch.setattr(wd, "_slots_in_use", _boom)
    state = wd.new_state()

    _run(_EABridge(version_ok=False), state, restarter, alert)

    assert restarter.calls == []
