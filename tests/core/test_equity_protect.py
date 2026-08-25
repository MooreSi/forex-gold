"""Tests for core_equity_protect.py -- check_equity_protect (loss direction,
regression after refactoring its grouping logic into a shared helper) and
check_basket_harvest (2026-08-12, profit-direction mirror).

Uses fakes for the bridge and close_trade_fn -- no real or demo MT5 order
is ever placed, closed, or modified.
"""
import asyncio
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_templates as ea_templates
from backend.src.services.positions.core_equity_protect import check_equity_protect, check_basket_harvest


@pytest.fixture
def fresh_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    os.remove(path)


class _FakeBridge:
    def __init__(self, positions):
        self._positions = positions

    async def get_positions(self):
        return self._positions


def _trade(trade_id, strategy, tg_source, ticket):
    return {"trade_id": trade_id, "strategy": strategy, "tg_source": tg_source, "mt5_ticket": ticket}


class _Closer:
    def __init__(self):
        self.calls = []

    async def __call__(self, trade_id, reason):
        self.calls.append((trade_id, reason))


def _save_template(name, **fields):
    return ea_templates.save_ea_template(name, fields)


# ── check_equity_protect (loss direction) ────────────────────────────────

def test_equity_protect_closes_group_when_combined_loss_exceeds_threshold(fresh_db):
    _save_template("Tpl", equity_protect=50.0)
    trades = [
        _trade("t1", "template:Tpl", "Chan", 101),
        _trade("t2", "template:Tpl", "Chan", 102),
    ]
    bridge = _FakeBridge([
        {"ticket": 101, "profit": -30.0},
        {"ticket": 102, "profit": -25.0},
    ])
    closer = _Closer()
    asyncio.run(check_equity_protect(trades, bridge, closer))
    assert sorted(c[0] for c in closer.calls) == ["t1", "t2"]
    assert all(c[1] == "equity_protect" for c in closer.calls)


def test_equity_protect_no_close_below_threshold(fresh_db):
    _save_template("Tpl", equity_protect=100.0)
    trades = [_trade("t1", "template:Tpl", "Chan", 101)]
    bridge = _FakeBridge([{"ticket": 101, "profit": -30.0}])
    closer = _Closer()
    asyncio.run(check_equity_protect(trades, bridge, closer))
    assert closer.calls == []


def test_equity_protect_ignores_non_template_strategy(fresh_db):
    trades = [_trade("t1", "scale_out", "Chan", 101)]
    bridge = _FakeBridge([{"ticket": 101, "profit": -1000.0}])
    closer = _Closer()
    asyncio.run(check_equity_protect(trades, bridge, closer))
    assert closer.calls == []


# ── check_basket_harvest (profit direction) ──────────────────────────────

def test_basket_harvest_closes_group_when_combined_profit_reaches_threshold(fresh_db):
    _save_template("Staged Ratchet 100-500", basket_harvest_threshold=500.0)
    trades = [
        _trade("t1", "template:Staged Ratchet 100-500", "GOLD DIGGERS INSTITUTIONAL", 201),
        _trade("t2", "template:Staged Ratchet 100-500", "GOLD DIGGERS INSTITUTIONAL", 202),
        _trade("t3", "template:Staged Ratchet 100-500", "GOLD DIGGERS INSTITUTIONAL", 203),
    ]
    bridge = _FakeBridge([
        {"ticket": 201, "profit": 200.0},
        {"ticket": 202, "profit": 150.0},
        {"ticket": 203, "profit": 151.0},
    ])
    closer = _Closer()
    asyncio.run(check_basket_harvest(trades, bridge, closer))
    assert sorted(c[0] for c in closer.calls) == ["t1", "t2", "t3"]
    assert all(c[1] == "basket_harvest" for c in closer.calls)


def test_basket_harvest_no_close_below_threshold(fresh_db):
    _save_template("Staged Ratchet 100-500", basket_harvest_threshold=500.0)
    trades = [
        _trade("t1", "template:Staged Ratchet 100-500", "GOLD DIGGERS INSTITUTIONAL", 201),
        _trade("t2", "template:Staged Ratchet 100-500", "GOLD DIGGERS INSTITUTIONAL", 202),
    ]
    bridge = _FakeBridge([
        {"ticket": 201, "profit": 200.0},
        {"ticket": 202, "profit": 299.0},
    ])
    closer = _Closer()
    asyncio.run(check_basket_harvest(trades, bridge, closer))
    assert closer.calls == []


def test_basket_harvest_off_by_default(fresh_db):
    _save_template("Staged Ratchet 100-500")  # basket_harvest_threshold defaults to 0.0
    trades = [_trade("t1", "template:Staged Ratchet 100-500", "Chan", 201)]
    bridge = _FakeBridge([{"ticket": 201, "profit": 100000.0}])
    closer = _Closer()
    asyncio.run(check_basket_harvest(trades, bridge, closer))
    assert closer.calls == []


def test_basket_harvest_groups_independently_per_channel(fresh_db):
    """Two channels sharing one template -- one crossing its combined
    profit threshold must not touch the other channel's position."""
    _save_template("Shared Tpl", basket_harvest_threshold=100.0)
    trades = [
        _trade("t1", "template:Shared Tpl", "Chan A", 301),
        _trade("t2", "template:Shared Tpl", "Chan B", 302),
    ]
    bridge = _FakeBridge([
        {"ticket": 301, "profit": 150.0},  # Chan A over threshold
        {"ticket": 302, "profit": 10.0},   # Chan B well under
    ])
    closer = _Closer()
    asyncio.run(check_basket_harvest(trades, bridge, closer))
    assert closer.calls == [("t1", "basket_harvest")]


def test_basket_harvest_ignores_position_missing_from_bridge(fresh_db):
    """A trade whose ticket the bridge no longer reports (already closed
    elsewhere) must not be counted toward the group total or double-closed."""
    _save_template("Tpl", basket_harvest_threshold=100.0)
    trades = [
        _trade("t1", "template:Tpl", "Chan", 401),
        _trade("t2", "template:Tpl", "Chan", 402),
    ]
    bridge = _FakeBridge([{"ticket": 401, "profit": 50.0}])  # 402 not reported
    closer = _Closer()
    asyncio.run(check_basket_harvest(trades, bridge, closer))
    assert closer.calls == []  # 50.0 < 100.0 threshold, 402 excluded entirely
