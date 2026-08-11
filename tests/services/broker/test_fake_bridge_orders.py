"""FakeMT5Bridge order lifecycle against its internal ledger.

The fake must honour the real bridge's dict conventions exactly — success
dicts with `ticket`/`fill_price`, `{"error": ...}` on refusal, never an
exception where the real client returns a value — and must be able to
misbehave on demand (error injection) so rejection paths are testable.

No test in this file can reach a broker: the ledger is an in-memory dict;
there is no network code in the fake at all.
"""
from __future__ import annotations

import asyncio

from backend.src.services.broker.fake_bridge import FakeMT5Bridge

BASE = 1_700_000_000.0


def _bridge(ts: float = BASE) -> FakeMT5Bridge:
    return FakeMT5Bridge(seed=42, clock=lambda: ts, starting_balance=1000.0)


def test_order_lifecycle():
    """Place → visible in get_positions; close → gone, deal recorded,
    balance moves by the realised profit."""
    bridge = _bridge()
    tick = asyncio.run(bridge.get_tick())

    result = asyncio.run(bridge.place_order("buy", 0.05, 2390.0, 2415.0, "sig:test"))
    assert "error" not in result
    ticket = result["ticket"]
    assert isinstance(ticket, int)
    assert result["fill_price"] == tick.ask
    assert result["direction"] == "BUY"

    positions = asyncio.run(bridge.get_positions())
    assert len(positions) == 1
    pos = positions[0]
    assert pos["ticket"] == ticket
    assert pos["symbol"] == "XAUUSD"
    assert pos["type"] == "BUY"
    assert pos["volume"] == 0.05
    assert pos["open_price"] == tick.ask
    assert pos["sl"] == 2390.0 and pos["tp"] == 2415.0

    closed = asyncio.run(bridge.close_position(ticket))
    assert closed["success"] is True
    assert closed["close_price"] == tick.bid  # BUY closes at bid
    assert asyncio.run(bridge.get_positions()) == []

    deals = asyncio.run(bridge.get_deal_history(1))
    out_deals = [d for d in deals if d["entry"] == 1]
    assert len(out_deals) == 1
    expected_profit = round((tick.bid - tick.ask) * 0.05 * 100.0, 2)
    assert out_deals[0]["profit"] == expected_profit
    assert out_deals[0]["position_id"] == ticket

    account = asyncio.run(bridge.get_account())
    assert account["balance"] == round(1000.0 + expected_profit, 2)


def test_partial_close_and_modify():
    bridge = _bridge()
    ticket = asyncio.run(bridge.place_order("sell", 0.10, 2415.0, 2385.0))["ticket"]

    partial = asyncio.run(bridge.partial_close(ticket, 0.04))
    assert partial["success"] is True
    assert partial["lots_closed"] == 0.04
    assert partial["remaining"] == 0.06
    pos = asyncio.run(bridge.get_positions())[0]
    assert pos["volume"] == 0.06

    modified = asyncio.run(bridge.modify_order(ticket, 2410.0, 2390.0))
    assert modified["success"] is True
    pos = asyncio.run(bridge.get_positions())[0]
    assert pos["sl"] == 2410.0 and pos["tp"] == 2390.0


def test_unknown_ticket_returns_error_dicts_not_exceptions():
    bridge = _bridge()
    assert "error" in asyncio.run(bridge.close_position(99999999))
    assert "error" in asyncio.run(bridge.partial_close(99999999, 0.01))
    assert "error" in asyncio.run(bridge.modify_order(99999999, 1.0, 2.0))
    assert asyncio.run(bridge.get_position_history(99999999)) == []


def test_error_injection_returns_error_dict_and_no_state_change():
    bridge = _bridge()
    bridge.inject_error("place_order", {"error": "Market is closed", "retcode": 10018})

    refused = asyncio.run(bridge.place_order("buy", 0.05, None, None))
    assert refused == {"error": "Market is closed", "retcode": 10018}
    assert asyncio.run(bridge.get_positions()) == []

    # Negative control: the injection is consumed — the same call now fills.
    ok = asyncio.run(bridge.place_order("buy", 0.05, None, None))
    assert "ticket" in ok
    assert len(asyncio.run(bridge.get_positions())) == 1


def test_open_position_profit_tracks_the_scripted_price():
    """Equity/profit move with the scenario curve — the demo has to visibly
    make and lose money."""
    scenario = {"anchors": [[0, 2400.0], [100, 2410.0]]}
    clock = {"now": BASE}
    bridge = FakeMT5Bridge(seed=1, scenario=scenario, base_ts=BASE,
                           clock=lambda: clock["now"], starting_balance=1000.0)
    ticket = asyncio.run(bridge.place_order("buy", 0.05, None, None))["ticket"]

    clock["now"] = BASE + 100  # mid moved +10.00
    pos = asyncio.run(bridge.get_positions())[0]
    assert pos["ticket"] == ticket
    assert pos["profit"] > 0
    account = asyncio.run(bridge.get_account())
    assert account["equity"] > account["balance"]


def test_broker_side_sl_execution_settles_the_position():
    """MT5 executes SL/TP server-side; the fake must too, or the monitor's
    defer-to-broker reconciliation waits forever. Exact fill at the level."""
    scenario = {"anchors": [[0, 2400.0], [100, 2380.0]]}
    clock = {"now": BASE}
    bridge = FakeMT5Bridge(seed=1, scenario=scenario, base_ts=BASE,
                           clock=lambda: clock["now"], starting_balance=1000.0)
    ticket = asyncio.run(bridge.place_order("buy", 0.05, 2392.0, None))["ticket"]

    clock["now"] = BASE + 100  # bid ~2379.85 — far through the 2392 stop
    assert asyncio.run(bridge.get_positions()) == []
    deals = asyncio.run(bridge.get_deal_history(1))
    out = [d for d in deals if d["position_id"] == ticket and d["entry"] == 1]
    assert len(out) == 1
    assert out[0]["price"] == 2392.0          # exact fill at the SL level
    assert out[0]["comment"] == "[sl]"
    assert out[0]["profit"] < 0
    # Negative control: without a crossed level nothing settles.
    ticket2 = asyncio.run(bridge.place_order("buy", 0.05, 2300.0, None))["ticket"]
    assert [p["ticket"] for p in asyncio.run(bridge.get_positions())] == [ticket2]


def test_passive_lifecycle_methods_are_benign():
    bridge = _bridge()
    asyncio.run(bridge.startup())
    health = asyncio.run(bridge.get_health())
    assert health["connected"] is True
    assert health["trade_allowed"] is True
    assert "error" not in asyncio.run(bridge.enable_autotrading())
    assert "error" not in asyncio.run(bridge.reconnect())
    assert "error" not in asyncio.run(bridge.send_credentials(1, "x", "srv"))
    asyncio.run(bridge.shutdown())
