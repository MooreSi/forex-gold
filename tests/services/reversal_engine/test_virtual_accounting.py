"""Quote, cost and setup-family regressions. No database or broker is reachable."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from backend.src.services.reversal_engine import reversal_engine_manage as m


def test_renamed_unicorn_uses_its_immutable_setup_type():
    e = SimpleNamespace(_manage_gd2_signal=AsyncMock(), _manage_conservative_signal=AsyncMock(),
                        _manage_ref_ladder_signal=AsyncMock())
    asyncio.run(m._ManagementMixin._manage_triggered_signal(e,
        {"level_type": "unicorn", "source_channel": "Renamed again", "strategy": "scale_out"}, None))
    e._manage_gd2_signal.assert_awaited_once()
    e._manage_ref_ladder_signal.assert_not_awaited()


def test_ordinary_level_still_uses_the_regular_ladder():
    e = SimpleNamespace(_manage_gd2_signal=AsyncMock(), _manage_conservative_signal=AsyncMock(),
                        _manage_ref_ladder_signal=AsyncMock())
    asyncio.run(m._ManagementMixin._manage_triggered_signal(e,
        {"level_type": "swing_low", "strategy": "scale_out"}, None))
    e._manage_ref_ladder_signal.assert_awaited_once()
    e._manage_gd2_signal.assert_not_awaited()


@pytest.mark.parametrize("spread", [0, .3, 1.2])
def test_executable_prices_only_pay_incremental_costs(spread):
    e = SimpleNamespace(_main_eng=SimpleNamespace(calculate_fees=lambda *a:
        {"total_cost": spread * 10 + 2.5, "spread_cost": spread * 10}))
    gross, net = m._ManagementMixin._net_pnl(e, {}, -spread, SimpleNamespace(spread=spread))
    assert gross == pytest.approx(-spread * 10)
    assert net == pytest.approx(gross - 2.5)


@pytest.mark.parametrize("direction,bid,ask,stop", [("BUY", 94.9, 95.3, 95), ("SELL", 104.7, 105.1, 105)])
def test_virtual_stop_reads_the_executable_quote(monkeypatch, direction, bid, ask, stop):
    calls = []
    monkeypatch.setattr(m.re_db, "record_excursion", lambda *a: None)
    monkeypatch.setattr(m.re_db, "close_signal", lambda *a, **k: calls.append((a, k)))
    monkeypatch.setattr("backend.src.services.reversal_engine.ml_engine.record_outcome", lambda *a: None)
    monkeypatch.setattr("backend.src.db.database.close_bus_entry", lambda *a: None)
    monkeypatch.setattr("backend.src.services.cluster.sync.ledger.push_trade_closed", lambda *a: None)
    e = m._ManagementMixin()
    e._main_eng = None
    e._notify_refresh = Mock()
    sig = {"id": 1, "direction": direction, "stop_loss": stop, "tp1": 110 if direction == "BUY" else 90,
           "entry_low": 99, "entry_high": 101, "trigger_price": 100, "remaining_frac": 1}
    asyncio.run(e._manage_ref_ladder_signal(sig, SimpleNamespace(bid=bid, ask=ask, mid=(bid + ask) / 2)))
    assert len(calls) == 1
    assert calls[0][0][2] == "loss"
