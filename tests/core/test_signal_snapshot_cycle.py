"""One tick of the signal-snapshot loop.

Three cadences share the tick: the per-signal capture every 5s, background
negatives every 15 minutes, and the pro-outcome resolve every 60s. The gating
between them, and the fact that each is isolated from the others' failures,
had no test. It is moving out of runtime.py, so it gets pinned first.

The isolation matters more than the cadence. This is a research log, and the
whole reason it polls rather than hooking the parser is that it must never be
able to break signal processing.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_signal_snapshot as snap


@pytest.fixture
def spy(monkeypatch):
    calls = {"capture": 0, "background": 0, "resolve": 0}

    async def _cap(bridge):
        calls["capture"] += 1
        return 0

    async def _bg(bridge):
        calls["background"] += 1
        return 0

    async def _res(bridge):
        calls["resolve"] += 1
        return 0

    monkeypatch.setattr(snap, "capture_pending_snapshots", _cap)
    monkeypatch.setattr(snap, "capture_background_snapshot", _bg)
    from backend.src.services.reversal_engine import pro_outcome
    monkeypatch.setattr(pro_outcome, "resolve_pending", _res)
    return calls


def _tick(state, now=0.0):
    return asyncio.run(snap.run_snapshot_cycle(state, bridge=object(), now=now))


class TestCadence:
    def test_capture_runs_every_tick(self, spy):
        st = snap.SnapshotState()
        _tick(st, now=0.0)
        _tick(st, now=5.0)
        assert spy["capture"] == 2

    def test_background_runs_on_the_first_tick(self, spy):
        _tick(snap.SnapshotState(), now=1000.0)
        assert spy["background"] == 1

    def test_background_waits_fifteen_minutes(self, spy):
        st = snap.SnapshotState()
        _tick(st, now=1000.0)
        _tick(st, now=1000.0 + 899)
        assert spy["background"] == 1
        _tick(st, now=1000.0 + 901)
        assert spy["background"] == 2

    def test_resolve_waits_sixty_seconds(self, spy):
        st = snap.SnapshotState()
        _tick(st, now=1000.0)
        _tick(st, now=1000.0 + 59)
        assert spy["resolve"] == 1
        _tick(st, now=1000.0 + 61)
        assert spy["resolve"] == 2

    def test_the_two_slow_cadences_are_independent(self, spy):
        """60s and 900s, not one gate for both."""
        st = snap.SnapshotState()
        _tick(st, now=1000.0)
        _tick(st, now=1000.0 + 100)
        assert (spy["resolve"], spy["background"]) == (2, 1)


class TestIsolation:
    """A research log must never be able to break signal processing, and the
    three parts must not be able to break each other."""

    def test_a_failed_capture_does_not_stop_the_others(self, spy, monkeypatch):
        async def _boom(bridge):
            raise RuntimeError("no bridge")
        monkeypatch.setattr(snap, "capture_pending_snapshots", _boom)
        _tick(snap.SnapshotState(), now=1000.0)
        assert (spy["background"], spy["resolve"]) == (1, 1)

    def test_a_failed_background_does_not_stop_the_resolve(self, spy, monkeypatch):
        async def _boom(bridge):
            raise RuntimeError("no candles")
        monkeypatch.setattr(snap, "capture_background_snapshot", _boom)
        _tick(snap.SnapshotState(), now=1000.0)
        assert spy["resolve"] == 1

    def test_a_failed_resolve_does_not_propagate(self, spy, monkeypatch):
        async def _boom(bridge):
            raise RuntimeError("corpus locked")
        from backend.src.services.reversal_engine import pro_outcome
        monkeypatch.setattr(pro_outcome, "resolve_pending", _boom)
        _tick(snap.SnapshotState(), now=1000.0)
        assert spy["capture"] == 1

    def test_a_failed_slow_cadence_still_advances_its_clock(self, spy, monkeypatch):
        """Otherwise a persistent failure retries every 5s instead of every
        15 minutes -- the same credit-burn shape as the auto-template loop."""
        async def _boom(bridge):
            spy["background"] += 1
            raise RuntimeError("no candles")
        monkeypatch.setattr(snap, "capture_background_snapshot", _boom)
        st = snap.SnapshotState()
        _tick(st, now=1000.0)
        _tick(st, now=1005.0)
        assert spy["background"] == 1
