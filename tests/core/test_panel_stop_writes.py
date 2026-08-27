"""When the panel records a moved stop-loss.

`core_bot_panel` holds the last money-path SQL outside the data layer: two
`UPDATE vantage_simulated_trades SET stop_loss=?` writes, in Risk Free and Push
SL. They are about to move into a repo, so what governs them is pinned first.

The property that matters is the ORDER. Both call `bridge.modify_order` and
only record the new stop **if the broker accepted it**. Reversing that, or
recording on failure, leaves the app believing a position is protected at a
price the broker never set -- which is invisible until the market reaches it.

Push SL also refuses a stop that would land at or past current price. That is
not a tighter stop; sent to a broker it is either rejected or filled as an
instant close.

No broker: `ctx._bridge` is a stub. Nothing here modifies a real order.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from backend.src.services.positions import core_bot_panel as panel
# _push_sl_one lives in _panel_trade_ops and binds _trade_push_sl_pips at
# import, so a patch has to land there -- patching the name re-exported from
# core_bot_panel does not reach the module that calls it.
from backend.src.services.positions import _panel_trade_ops


def _bridge(*, modify=None, positions=(), raises=False):
    calls = []

    async def modify_order(ticket, sl, tp):
        calls.append({"ticket": ticket, "sl": sl, "tp": tp})
        if raises:
            raise RuntimeError("bridge down")
        return modify if modify is not None else {}

    async def get_positions():
        return list(positions)

    ns = types.SimpleNamespace(modify_order=modify_order, get_positions=get_positions)
    ns.calls = calls
    return ns


def _ctx(bridge):
    return types.SimpleNamespace(_bridge=bridge)


@pytest.fixture
def channel(monkeypatch):
    """The panel resolves a slug against the configured channel list, so a
    trade alone is not enough to reach the code under test."""
    monkeypatch.setattr(
        panel.db_module, "get_all_channel_strategy_overrides",
        lambda: {"GD VIP": {"strategy": "scale_out"}}, raising=False)
    return "GD VIP"


def _trade(conn, *, ticket=111, entry=2400.0, sl=2390.0, source="GD VIP",
           strategy="scale_out", direction="BUY"):
    tid = uuid.uuid4().hex[:16]
    sid = f"sig-{tid}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", (sid, direction, entry - 1, entry + 1, sl))
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, "
        " entry_price, lot_size, remaining_lots, stop_loss, status, open_time, "
        " net_pnl, tg_source, strategy) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'open',0,0,?,?)",
        (tid, sid, ticket, direction, entry - 1, entry + 1, entry, 0.1, 0.1,
         sl, source, strategy))
    return tid


def _stop(db, tid):
    with db.db() as conn:
        return conn.execute(
            "SELECT stop_loss FROM vantage_simulated_trades WHERE trade_id=?",
            (tid,)).fetchone()[0]


# ── Risk Free ─────────────────────────────────────────────────────────────────

def test_risk_free_records_the_stop_only_after_the_broker_accepts(fresh_db, channel):
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=111, entry=2400.0, sl=2390.0)

    bridge = _bridge(modify={})
    asyncio.run(panel._risk_free(panel._slug("GD VIP"), _ctx(bridge)))

    assert bridge.calls and bridge.calls[0]["sl"] == 2400.0, "broker moved to entry"
    assert _stop(fresh_db, tid) == pytest.approx(2400.0)


def test_risk_free_does_not_record_a_stop_the_broker_refused(fresh_db, channel):
    """The app must never believe a position is protected at a price the
    broker never set."""
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=111, entry=2400.0, sl=2390.0)

    bridge = _bridge(modify={"error": "invalid stops"})
    asyncio.run(panel._risk_free(panel._slug("GD VIP"), _ctx(bridge)))

    assert _stop(fresh_db, tid) == pytest.approx(2390.0), "the stored stop must not move"


def test_risk_free_survives_a_bridge_that_raises(fresh_db, channel):
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=111, entry=2400.0, sl=2390.0)

    screen = asyncio.run(panel._risk_free(panel._slug("GD VIP"), _ctx(_bridge(raises=True))))

    assert _stop(fresh_db, tid) == pytest.approx(2390.0)
    assert screen.mode in ("send", "noop"), "it must report, not raise"


def test_risk_free_skips_a_leg_with_no_ticket_yet(fresh_db, channel):
    """A staged template leg has nothing at the broker to protect."""
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=0, entry=2400.0, sl=2390.0)

    bridge = _bridge(modify={})
    asyncio.run(panel._risk_free(panel._slug("GD VIP"), _ctx(bridge)))

    assert bridge.calls == []
    assert _stop(fresh_db, tid) == pytest.approx(2390.0)


# ── Push SL ───────────────────────────────────────────────────────────────────

def _pos(ticket=111, sl=2390.0, price=2405.0):
    return {"ticket": ticket, "sl": sl, "current_price": price}


def test_push_sl_refuses_a_stop_at_or_past_current_price(fresh_db, monkeypatch):
    """Not a tighter stop -- a broker either rejects it or fills it as an
    instant close."""
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=111, entry=2400.0, sl=2404.0)
    monkeypatch.setattr(_panel_trade_ops, "_trade_push_sl_pips", lambda t: 50.0)

    bridge = _bridge(modify={}, positions=[_pos(sl=2404.0, price=2405.0)])
    screen = asyncio.run(panel._push_sl_one(tid[:8], _ctx(bridge)))

    assert bridge.calls == [], "nothing should reach the broker"
    assert _stop(fresh_db, tid) == pytest.approx(2404.0)
    assert "refused" in (screen.toast or "").lower()


def test_push_sl_does_not_record_a_stop_the_broker_refused(fresh_db, monkeypatch):
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=111, entry=2400.0, sl=2390.0)
    monkeypatch.setattr(_panel_trade_ops, "_trade_push_sl_pips", lambda t: 1.0)

    bridge = _bridge(modify={"error": "invalid stops"}, positions=[_pos()])
    asyncio.run(panel._push_sl_one(tid[:8], _ctx(bridge)))

    assert _stop(fresh_db, tid) == pytest.approx(2390.0)


def test_push_sl_records_the_stop_the_broker_accepted(fresh_db, monkeypatch):
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=111, entry=2400.0, sl=2390.0)
    monkeypatch.setattr(_panel_trade_ops, "_trade_push_sl_pips", lambda t: 1.0)

    bridge = _bridge(modify={}, positions=[_pos()])
    asyncio.run(panel._push_sl_one(tid[:8], _ctx(bridge)))

    assert bridge.calls, "the broker should have been asked"
    assert _stop(fresh_db, tid) == pytest.approx(bridge.calls[0]["sl"]), (
        "the recorded stop must be exactly what the broker was given"
    )


def test_push_sl_is_unavailable_without_a_configured_push(fresh_db, monkeypatch):
    with fresh_db.db() as conn:
        tid = _trade(conn, ticket=111)
    monkeypatch.setattr(_panel_trade_ops, "_trade_push_sl_pips", lambda t: 0.0)

    screen = asyncio.run(panel._push_sl_one(tid[:8], _ctx(_bridge())))
    assert screen.mode == "noop"
