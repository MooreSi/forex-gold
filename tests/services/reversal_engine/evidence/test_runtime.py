"""Task lifecycle only; bridge sentinel is never called."""
import asyncio
from types import SimpleNamespace
from backend.src.services.reversal_engine.evidence.runtime import collector


def test_collector_starts_and_cancels_child_without_interfering_with_owner():
    state = []
    async def worker(engine, running):
        state.append("started")
        try:
            await asyncio.Event().wait()
        finally:
            state.append("cancelled")
    async def exercise():
        async with collector(SimpleNamespace(_bridge=object()), lambda: True, worker):
            await asyncio.sleep(0)
            state.append("owner ran")
    asyncio.run(exercise())
    assert state == ["started", "owner ran", "cancelled"]


def test_engine_without_bridge_does_not_start_background_work():
    state = []
    async def worker(engine, running): state.append("called")
    async def exercise():
        async with collector(object(), lambda: True, worker):
            await asyncio.sleep(0)
    asyncio.run(exercise())
    assert state == []


def test_tick_worker_uses_only_read_methods_and_persists_quotes(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.src.services.reversal_engine.evidence import runtime
    from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore
    store = EvidenceStore(tmp_path / "e.db")
    monkeypatch.setattr(runtime, "paths", lambda: ("demo", tmp_path, tmp_path / "main.db"))
    monkeypatch.setattr(runtime, "time", SimpleNamespace(time=lambda: 100))
    bridge = SimpleNamespace(get_fresh_tick=AsyncMock(return_value=SimpleNamespace(bid=100,ask=101,timestamp=99)),
        get_ticks_range=AsyncMock(return_value=[{"time": 95, "bid": 99, "ask": 100}]))
    asyncio.run(runtime.Worker(SimpleNamespace(_bridge=bridge), store).ticks())
    assert len(store.events("demo", 100)) == 2
    bridge.get_ticks_range.assert_awaited_once_with(70, 100)
    bridge.get_fresh_tick.assert_awaited_once()


def test_account_switch_during_quote_fetch_discards_ambiguous_data(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.src.services.reversal_engine.evidence import runtime
    from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore
    store = EvidenceStore(tmp_path / "e.db")
    calls = iter([("demo", tmp_path, tmp_path / "demo.db"), ("live", tmp_path, tmp_path / "live.db")])
    monkeypatch.setattr(runtime, "paths", lambda: next(calls))
    monkeypatch.setattr(runtime, "time", SimpleNamespace(time=lambda: 100))
    bridge = SimpleNamespace(get_fresh_tick=AsyncMock(return_value=SimpleNamespace(bid=100,ask=101,timestamp=99)))
    asyncio.run(runtime.Worker(SimpleNamespace(_bridge=bridge), store).ticks())
    assert store.events("demo", 100) == []
    assert store.events("live", 100) == []


def test_broken_tick_history_does_not_discard_valid_fresh_quote(tmp_path, monkeypatch):
    from unittest.mock import AsyncMock
    from backend.src.services.reversal_engine.evidence import runtime
    from backend.src.services.reversal_engine.evidence.evidence_repo import EvidenceStore
    store = EvidenceStore(tmp_path / "e.db")
    monkeypatch.setattr(runtime, "paths", lambda: ("demo", tmp_path, tmp_path / "main.db"))
    monkeypatch.setattr(runtime, "time", SimpleNamespace(time=lambda: 100))
    bridge = SimpleNamespace(get_fresh_tick=AsyncMock(return_value=SimpleNamespace(bid=100,ask=101,timestamp=99)),
        get_ticks_range=AsyncMock(side_effect=RuntimeError("history unavailable")))
    asyncio.run(runtime.Worker(SimpleNamespace(_bridge=bridge), store).ticks())
    assert len(store.events("demo", 100)) == 1
    assert store.status()["broker_tick_range"]["state"] == "unavailable"
