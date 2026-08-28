"""broker_controller deliberately does not hand out the EA bridge.

Three pages used to call ea_bridge.get_instance() and then work on the live
object -- the diagnostics panel read `_ea._last_seen`, a private attribute, to
build a status string. Exposing get_instance() through a controller would have
satisfied the import contract while leaving that exactly as it was, so the
operations are exposed and the instance stays behind them.

That means these are not pure forwarders: ea_is_healthy() and
ea_seconds_since_last_seen() collapse logic the pages were writing by hand, and
push_template()/push_global_config() decide what to do when no EA is connected.
Reshaped logic gets tests.

Nothing here touches a real bridge or a real EA.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.controllers import broker_controller as bc


class _FakeEA:
    def __init__(self, *, healthy=True, last_seen=None):
        self._healthy = healthy
        self._last_seen = time.time() if last_seen is None else last_seen
        self.pushed_global = 0

    def is_ea_healthy(self) -> bool:
        return self._healthy

    async def push_global_config(self):
        self.pushed_global += 1


@pytest.fixture
def no_ea(monkeypatch):
    monkeypatch.setattr(bc._bridge, "get_instance", lambda: None)


@pytest.fixture
def ea(monkeypatch):
    inst = _FakeEA()
    monkeypatch.setattr(bc._bridge, "get_instance", lambda: inst)
    return inst


class TestEaIsHealthy:
    def test_no_bridge_at_all_is_not_healthy(self, no_ea):
        """The pages wrote `ea is None or not ea.is_ea_healthy()`. Losing the
        None arm would raise AttributeError on a fresh start, before any EA has
        ever connected."""
        assert bc.ea_is_healthy() is False

    def test_a_connected_but_unhealthy_ea_is_not_healthy(self, monkeypatch):
        monkeypatch.setattr(bc._bridge, "get_instance", lambda: _FakeEA(healthy=False))
        assert bc.ea_is_healthy() is False

    def test_a_healthy_ea_is_healthy(self, ea):
        assert bc.ea_is_healthy() is True


class TestEaSecondsSinceLastSeen:
    def test_no_bridge_reads_as_never_seen(self, no_ea):
        assert bc.ea_seconds_since_last_seen() is None

    def test_a_bridge_that_never_heard_an_ea_reads_as_never_seen(self, monkeypatch):
        """_last_seen == 0 is the sentinel for "no EA has spoken this session".
        Returning 0.0 here instead of None would make the panel say the
        connection was lost 1756000000 seconds ago."""
        monkeypatch.setattr(bc._bridge, "get_instance", lambda: _FakeEA(last_seen=0))
        assert bc.ea_seconds_since_last_seen() is None

    def test_it_reports_the_age_not_the_timestamp(self, monkeypatch):
        monkeypatch.setattr(bc._bridge, "get_instance",
                            lambda: _FakeEA(last_seen=time.time() - 42))
        age = bc.ea_seconds_since_last_seen()
        assert age == pytest.approx(42, abs=2)


class TestPushTemplate:
    def test_it_sends_when_an_ea_is_connected(self, ea, monkeypatch):
        sent = {}
        monkeypatch.setattr(bc._bridge, "schedule_push_template",
                            lambda inst, name, values: sent.update(name=name, values=values))
        assert bc.push_template("Grid A", {"sl": 40}) is True
        assert sent == {"name": "Grid A", "values": {"sl": 40}}

    def test_it_reports_false_rather_than_raising_with_no_ea(self, no_ea, monkeypatch):
        """The page shows "values saved, will apply on next signal". Raising
        here would turn a normal disconnected state into an error toast."""
        monkeypatch.setattr(bc._bridge, "schedule_push_template",
                            lambda *a, **k: pytest.fail("must not send with no EA"))
        assert bc.push_template("Grid A", {"sl": 40}) is False


class TestPushGlobalConfig:
    def test_it_pushes_when_an_ea_is_connected(self, ea):
        assert asyncio.run(bc.push_global_config()) is True
        assert ea.pushed_global == 1

    def test_it_is_a_no_op_with_no_ea(self, no_ea):
        assert asyncio.run(bc.push_global_config()) is False


def test_the_controller_does_not_hand_out_the_bridge():
    """The whole point of the module. If get_instance ever appears here, a page
    can hold the live EABridge again and the boundary is decorative."""
    assert not hasattr(bc, "get_instance")
    assert "get_instance" not in bc.__all__
