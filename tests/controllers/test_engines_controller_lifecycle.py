"""The engine lifecycle surface (restructure phase1/010).

The panels and the app shell used to import the three engine service
singletons directly and loop over them choosing lifecycle — the exact
"page decides" failure the controller's docstring documents. These named
operations move that decision out of the caller.

**The table and the two bulk loops moved to `services/engines/registry.py` on
2026-09-18.** The Local/Remote handover needs them and a service may not import
a controller; the copy it grew instead was a second answer to "which engines
exist and which of them may be bulk-started". The controller still names
`get_engine`, `engines_running` and `sub_engines` for a router to call, and
those are exercised here through the controller, because that is the surface a
router sees.

Engines generate signals; they do not place orders. No test here can reach a
broker: the services are hand-written fakes recording calls.
"""
from __future__ import annotations

from backend.src.controllers import engines_controller as ec
from backend.src.services.engines import registry as reg


class _FakeEngine:
    def __init__(self, running: bool):
        self.is_running = running
        self.start_calls = 0
        self.stop_calls = 0

    def start(self):
        self.start_calls += 1
        self.is_running = True

    def stop(self):
        self.stop_calls += 1
        self.is_running = False


class _FakeService:
    def __init__(self, engine):
        self._engine = engine

    def get_instance(self):
        return self._engine


def _fake_world(monkeypatch, running: dict):
    engines = {name: _FakeEngine(state) for name, state in running.items()}
    monkeypatch.setattr(reg, "_ENGINE_SERVICES", {
        name: _FakeService(engine) for name, engine in engines.items()
    })
    return engines


def test_start_all_starts_only_stopped_engines(monkeypatch):
    engines = _fake_world(monkeypatch, {"breakout": False, "bounce": True, "reversal": False})
    reg.start_stopped()
    assert engines["breakout"].start_calls == 1
    assert engines["reversal"].start_calls == 1
    assert engines["bounce"].start_calls == 0  # already running — guard held


def test_the_fake_records_a_start_it_should_not_have_had(monkeypatch):
    """Negative control: the fake really can see a spurious start()."""
    engines = _fake_world(monkeypatch, {"breakout": True})
    engines["breakout"].start()
    assert engines["breakout"].start_calls == 1


def test_stop_all_stops_only_running_engines(monkeypatch):
    engines = _fake_world(monkeypatch, {"breakout": True, "bounce": False, "reversal": True})
    reg.stop_running()
    assert engines["breakout"].stop_calls == 1
    assert engines["reversal"].stop_calls == 1
    assert engines["bounce"].stop_calls == 0


def test_engines_running_reports_each_by_name(monkeypatch):
    _fake_world(monkeypatch, {"breakout": True, "bounce": False, "reversal": False})
    assert ec.engines_running() == {"breakout": True, "bounce": False, "reversal": False}


def test_get_engine_is_a_named_accessor(monkeypatch):
    engines = _fake_world(monkeypatch, {"breakout": False, "bounce": False, "reversal": False})
    assert ec.get_engine("breakout") is engines["breakout"]


def test_sub_engines_returns_the_fixed_bo_bc_gd_order(monkeypatch):
    """The mode toggle and the sync server bind (breakout, bounce, reversal)
    in exactly this order — reordering silently swaps which engine the VPS
    manages under each name."""
    engines = _fake_world(monkeypatch, {"breakout": False, "bounce": False, "reversal": False})
    assert ec.sub_engines() == (engines["breakout"], engines["bounce"], engines["reversal"])


def test_the_real_service_mapping_names_the_three_engines():
    assert set(reg._ENGINE_SERVICES) == {"breakout", "bounce", "reversal"}


def test_the_controller_and_the_registry_cannot_disagree():
    """One table. The controller re-exports the names so a router can render
    the tab from them, and this is what stops that becoming a third copy."""
    assert ec.ENGINE_NAMES == reg.ENGINE_NAMES
