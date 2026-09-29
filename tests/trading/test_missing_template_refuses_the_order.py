"""A `template:<name>` order is refused when this node has no such template.

Owner, 2026-09-29, after the VPS was found with an empty `ea_trade_templates`
table: 38 trades opened as `template:30 TP1 SL50 and Trail`, the lookup
returned None, and `open_trade` sent them to the EA with no template at all.
The EA ran them with no partial-close ladder: 18 reached the template's TP1
(+$4) and none took a partial. "refuse missing-template orders".

A missing template is not a lesser template: the EA has nothing to manage the
trade with, and there is no Python fallback for a template (the same reason
`open_trade` already refuses one when no EA is reachable).

Nothing here reaches a broker. The bridge and the EA are fakes whose order
calls only record; the assertions are that they recorded nothing.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import SimulationEngine, TradingRuntime
from backend.src.services.broker import ea_bridge, ea_templates, template_presence
from tests.core.test_open_trade_characterization import _FakeBridge


class _FakeEA:
    ea_version_ok = True

    def __init__(self):
        self.open_trade_calls = []

    def is_ea_healthy(self):
        return True

    def is_strategy_portable(self, strategy):
        return True

    async def open_trade(self, trade_id, direction, lot_size, stop_loss, tps, strategy,
                         **kwargs):
        self.open_trade_calls.append({"strategy": strategy, **kwargs})
        return {"type": "trade_opened", "ticket": 8001, "fill_price": 2400.7}


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge()
    ea = _FakeEA()
    ea_bridge.set_instance(ea)
    db.update_risk_settings({"ea_bridge_enabled": 1})
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-1", "BUY", 2399.0, 2401.0, 2390.0, "pending", time.time()),
        )
    yield e, ea
    ea_bridge.set_instance(None)


def _open(engine, strategy):
    return asyncio.run(SimulationEngine.open_trade(
        engine, signal_id="sig-1", direction="BUY", entry_low=2399.0,
        entry_high=2401.0, stop_loss=2390.0, tp1=2410.0, lot_size=0.10,
        strategy=strategy))


def _open_rows():
    with db.db() as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]


def test_a_template_this_node_does_not_have_is_refused(engine):
    rt, ea = engine

    with pytest.raises(RuntimeError, match="30 TP1 SL50 and Trail"):
        _open(rt, "template:30 TP1 SL50 and Trail")

    assert ea.open_trade_calls == []
    assert rt._bridge.place_order_calls == []
    assert _open_rows() == 0


def test_a_template_this_node_has_still_opens(engine):
    rt, ea = engine
    ea_templates.save_ea_template("Present", {"tp1_pips": 40.0, "tp1_pct": 55.0})

    result = _open(rt, "template:Present")

    assert result["trade_id"]
    assert len(ea_templates.get_ea_template("Present")) > 0
    assert [c["strategy"] for c in ea.open_trade_calls] == ["template:Present"]
    assert ea.open_trade_calls[0]["template"]["tp1_pct"] == 55.0


def test_a_non_template_strategy_is_not_asked_about_templates(fresh_db):
    assert template_presence.missing_template_reason("scale_out") is None
    assert template_presence.missing_template_reason(None) is None


def test_an_unreadable_template_library_refuses_rather_than_opens(fresh_db, monkeypatch):
    def _boom(name):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(ea_templates, "get_ea_template", _boom)

    reason = template_presence.missing_template_reason("template:X")

    assert reason is not None and "X" in reason
