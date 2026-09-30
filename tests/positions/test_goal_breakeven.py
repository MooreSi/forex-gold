"""Risk > Daily goal > "Move stops to breakeven once the goal is reached".

Owner, 2026-09-30: once the day's goal is reached with trades still open, move
each one's stop to breakeven so the open trades cannot take the day back under
it. Choices made the same day: breakeven is entry PLUS costs (so a stop-out
nets about $0, not minus the spread), and a trade not yet far enough in profit
is moved later, when it is, not closed.

Nothing here reaches a broker. The bridge is a fake whose `modify_order` is a
sentinel that records the call and returns a canned dict; realised profit is
closed rows in a throwaway database.
"""
import asyncio

import pytest

from backend.src.services.cluster import node_roles
from backend.src.services.positions import goal_breakeven as gb
from backend.src.services.positions import repo as positions_repo
from backend.src.services.positions.safety_net import compute_be_cost_pts

from tests.core.test_giveback_guard import _closes


class _Bridge:
    """Positions and a recording `modify_order`. Never a real broker."""

    def __init__(self, positions, reply=None):
        self.positions = positions
        self.reply = reply or {"success": True}
        self.modified = []

    async def get_positions(self):
        return self.positions

    async def get_account(self):
        return {"balance": 10_000.0}

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modified.append((int(ticket), sl, tp))
        return self.reply


def _pos(ticket=111, kind="BUY", entry=2400.0, price=2410.0, sl=2390.0):
    return {"ticket": ticket, "type": kind, "open_price": entry,
            "current_price": price, "sl": sl, "tp": 0.0, "volume": 0.1}


def _rs(protect=1, enabled=1, goal=100.0):
    return {"daily_goal_enabled": enabled, "daily_goal_mode": "usd",
            "daily_goal_value": goal, "daily_goal_protect_be": protect}


def _run(bridge, rs, open_trades=(), state=None, now=1_000.0):
    state = state or gb.SweepState()
    asyncio.run(gb.sweep(state, bridge, rs, lambda: list(open_trades), now=now))
    return state


@pytest.fixture
def recorded(monkeypatch):
    rows = []
    monkeypatch.setattr(positions_repo, "set_stop_loss_be",
                        lambda trade_id, sl: rows.append((trade_id, sl)))
    return rows


def _be(entry, kind="BUY"):
    cost = compute_be_cost_pts({})
    return round(entry + cost, 2) if kind == "BUY" else round(entry - cost, 2)


# ── when it acts ─────────────────────────────────────────────────────────────

def test_a_buy_in_profit_is_moved_to_entry_plus_costs_once_the_goal_is_reached(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge([_pos()])

    _run(bridge, _rs(), open_trades=[{"trade_id": "t-1", "mt5_ticket": 111}])

    assert bridge.modified == [(111, _be(2400.0), None)]
    assert recorded == [("t-1", _be(2400.0))]


def test_a_sell_is_moved_to_entry_minus_costs(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge([_pos(kind="SELL", entry=2400.0, price=2390.0, sl=2410.0)])

    _run(bridge, _rs())

    assert bridge.modified == [(111, _be(2400.0, "SELL"), None)]


def test_breakeven_is_beyond_the_entry_not_at_it(fresh_db, recorded):
    """Entry plus costs: a stop at the bare entry loses the spread and commission."""
    _closes([120])
    bridge = _Bridge([_pos()])

    _run(bridge, _rs())

    assert bridge.modified[0][1] > 2400.0


# ── when it must not ─────────────────────────────────────────────────────────

def test_nothing_moves_before_the_goal_is_reached(fresh_db, recorded):
    _closes([99])
    bridge = _Bridge([_pos()])

    _run(bridge, _rs())

    assert bridge.modified == []


def test_nothing_moves_with_the_tickbox_off(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge([_pos()])

    _run(bridge, _rs(protect=0))

    assert bridge.modified == []


def test_nothing_moves_with_the_goal_itself_off(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge([_pos()])

    _run(bridge, _rs(enabled=0))

    assert bridge.modified == []


def test_a_stop_already_at_or_past_breakeven_is_never_loosened(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge([_pos(sl=2405.0), _pos(ticket=222, kind="SELL", price=2390.0, sl=2395.0)])

    _run(bridge, _rs())

    assert bridge.modified == []


def test_a_trade_in_loss_waits_and_is_moved_once_in_profit(fresh_db, recorded):
    _closes([120])
    losing = _pos(price=2395.0)
    bridge = _Bridge([losing])

    state = _run(bridge, _rs(), now=1_000.0)
    assert bridge.modified == []

    losing["current_price"] = 2410.0
    _run(bridge, _rs(), state=state, now=1_000.0 + gb.SWEEP_EVERY_S)
    assert bridge.modified == [(111, _be(2400.0), None)]


def test_a_trade_only_just_past_breakeven_waits_for_room(fresh_db, recorded):
    """The bridge clamps a stop that sits too close to price, and a clamped stop
    can land looser than the one already there. So it waits for MIN_GAP."""
    _closes([120])
    bridge = _Bridge([_pos(price=_be(2400.0) + gb.MIN_GAP / 2)])

    _run(bridge, _rs())

    assert bridge.modified == []


def test_a_rejected_move_is_not_recorded_and_not_retried_every_sweep(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge([_pos()], reply={"error": "Invalid stops"})

    state = _run(bridge, _rs(), open_trades=[{"trade_id": "t-1", "mt5_ticket": 111}],
                 now=1_000.0)
    _run(bridge, _rs(), state=state, now=1_000.0 + gb.SWEEP_EVERY_S)
    assert len(bridge.modified) == 1
    assert recorded == []

    _run(bridge, _rs(), state=state, now=1_000.0 + gb.RETRY_S + 1)
    assert len(bridge.modified) == 2


def test_only_the_node_that_trades_moves_stops(fresh_db, recorded, monkeypatch):
    """Both nodes read one MT5 account; two nodes moving one stop is a race."""
    _closes([120])
    monkeypatch.setattr(node_roles, "is_active_trader_node", lambda: False)
    bridge = _Bridge([_pos()])

    _run(bridge, _rs())

    assert bridge.modified == []


def test_positions_mt5_cannot_report_are_left_alone(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge(None)

    _run(bridge, _rs())

    assert bridge.modified == []


def test_it_is_throttled(fresh_db, recorded):
    _closes([120])
    bridge = _Bridge([_pos()], reply={"error": "x"})
    state = _run(bridge, _rs(), now=1_000.0)
    bridge.positions = [_pos(ticket=222)]

    _run(bridge, _rs(), state=state, now=1_000.0 + 1)

    assert [m[0] for m in bridge.modified] == [111]
