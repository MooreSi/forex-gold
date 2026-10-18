"""Old incomplete broker evidence must remain recoverable; broker is a fake."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock
from backend.src.services.reversal_engine.evidence import runtime


def test_old_trade_without_deal_costs_is_not_permanently_abandoned(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "paths", lambda: ("demo", tmp_path, tmp_path / "main.db"))
    monkeypatch.setattr(runtime, "time", SimpleNamespace(time=lambda: 1_000_000))
    trade = {"mt5_ticket": 12, "status": "closed", "open_time": 10, "close_time": 100, "lot_size": .1}
    monkeypatch.setattr(runtime, "broker_rows", lambda *a: ([{"mt5_ticket": 12}], [trade], [], []))
    monkeypatch.setattr(runtime.broker_capture, "capture", lambda *a: {"labels": 0})
    store = SimpleNamespace(by_kind=lambda *a: [], put=Mock())
    bridge = SimpleNamespace(get_position_history=AsyncMock(return_value=[]))
    worker = runtime.Worker(SimpleNamespace(_bridge=bridge), store)
    asyncio.run(worker.broker())
    bridge.get_position_history.assert_awaited_once_with(12)


def test_backoff_and_oldest_attempt_order_prevent_newest_eight_starvation(tmp_path, monkeypatch):
    monkeypatch.setattr(runtime, "paths", lambda: ("demo", tmp_path, tmp_path / "main.db"))
    monkeypatch.setattr(runtime, "time", SimpleNamespace(time=lambda: 1_000_000))
    trades = [{"mt5_ticket": i, "status": "closed", "open_time": 10 + i, "close_time": 100, "lot_size": .1}
              for i in range(12)]
    monkeypatch.setattr(runtime, "broker_rows", lambda *a: ([{"mt5_ticket": i} for i in range(12)], trades, [], []))
    monkeypatch.setattr(runtime.broker_capture, "capture", lambda *a: {})
    bridge = SimpleNamespace(get_position_history=AsyncMock(return_value=[]))
    worker = runtime.Worker(SimpleNamespace(_bridge=bridge), SimpleNamespace(by_kind=lambda *a: [], put=Mock()))
    asyncio.run(worker.broker())
    asyncio.run(worker.broker())
    assert {c.args[0] for c in bridge.get_position_history.await_args_list} == set(range(12))
