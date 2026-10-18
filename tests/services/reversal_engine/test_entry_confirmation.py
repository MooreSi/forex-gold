"""M1 confirmation with a fake bridge: no order or production data is used."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

from backend.src.services.market.entry_trigger import TriggerConfig
from backend.src.services.reversal_engine import reversal_engine_live_execute as live


def test_forming_rejection_does_not_confirm_a_level(monkeypatch):
    assert confirmation(monkeypatch, [bar(99), bar(100.2)]) is not None


def test_completed_rejection_does_confirm_a_level(monkeypatch):
    assert confirmation(monkeypatch, [bar(99), bar(100.2), bar(99)]) is None


def test_unavailable_confirmation_is_observed_as_unknown(monkeypatch):
    from backend.src.services.market import entry_contract
    seen = []
    monkeypatch.setattr(entry_contract, "observe", lambda *a, **k: seen.append(a[4]))
    monkeypatch.setattr(live._caps, "entry_trigger_config", lambda rs: TriggerConfig(require_rejection=True))
    e = SimpleNamespace(_bridge=SimpleNamespace(get_candles=AsyncMock(side_effect=OSError("offline"))))
    reason = asyncio.run(live._LiveExecuteMixin._entry_trigger_blocks(e, {"id": 1}, {}))
    assert reason is None  # existing adapter failure policy is retained
    assert seen[-1]["confirmation_state"] == "unknown"


def bar(close):
    return {"open": 99, "high": 101, "low": 98, "close": close}


def confirmation(monkeypatch, bars):
    monkeypatch.setattr(live._caps, "entry_trigger_config", lambda rs: TriggerConfig(require_rejection=True))
    e = SimpleNamespace(_bridge=SimpleNamespace(get_candles=AsyncMock(return_value=bars)))
    return asyncio.run(live._LiveExecuteMixin._entry_trigger_blocks(e,
        {"level_price": 100, "atr": 5, "direction": "BUY"}, {}))
