"""Proves forex_trader.core.core_scan_messages_auto_execute.execute_auto_signal
behaves identically to SimulationEngine's original, characterized in
test_scan_messages_auto_execute_characterization.py -- see
docs/todo/refactor/core-scan-messages-auto-execute-migration/020-*.md.

Same assertions as 010, called through the new module instead of the
class. Highest real-money surface in the whole migration series -- always
faked here.
"""
import asyncio
import os
import tempfile
from types import SimpleNamespace
from unittest import mock

import pytest

from forex_trader.core import database as db
from forex_trader.core import ea_bridge
from forex_trader.core import core_scan_messages_auto_execute as ae


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def _get_last_signal():
    with db.db() as conn:
        return db.row_to_dict(
            conn.execute("SELECT * FROM vantage_signals ORDER BY created_at DESC LIMIT 1").fetchone()
        )


class _FakeBridge:
    def __init__(self, tick):
        self._tick = tick
        self.modify_calls = []

    async def get_tick(self):
        return self._tick

    async def modify_order(self, ticket, sl=None, tp=None):
        self.modify_calls.append((ticket, sl, tp))
        return {"success": True}


_PARSED = {"direction": "BUY", "entry_low": 4529.0, "entry_high": 4534.0, "stop_loss": 4527.0,
           "tp1": 4537.0, "tp2": 4539.0, "tp3": 4541.0, "tp4": 4543.0, "tp5": 4545.0,
           "tp6": None, "tp7": None, "tp8": None}
_IN_ZONE_TICK = SimpleNamespace(bid=4530.0, ask=4531.0)


async def _default_open_trade(**kwargs):
    _default_open_trade.calls.append(kwargs)
    return {"trade_id": "trade-xyz", "entry_price": kwargs.get("entry_low", 4530.0),
            "mt5_ticket": 999, "managed_by": "python"}
_default_open_trade.calls = []


async def _default_followup(channel_name, direction, parsed, tg_id):
    return False


async def _default_balance():
    return 1000.0


def _default_lot(entry, sl, balance, risk_pct):
    return 0.01


def _call(rs=None, tick=_IN_ZONE_TICK, strategy="scale_out", open_trade_fn=None,
         open_trades=None, filter_err=None, followup_matched=False,
         source_label="TestChannel", sess_ok=True, per_signal_skip=False,
         parsed=None):
    rs = rs or {}
    _default_open_trade.calls = []
    bridge = _FakeBridge(tick)

    async def followup_fn(channel_name, direction, p, tg_id):
        return followup_matched

    result = asyncio.run(ae.execute_auto_signal(
        parsed or dict(_PARSED), "tg1", "TestChannel", source_label, strategy, rs,
        sess_ok, per_signal_skip, "AI declined", "Auto-execution is OFF — activate manually in the dashboard.",
        bridge,
        get_open_trades_fn=lambda: open_trades or [],
        find_and_apply_instant_followup_fn=followup_fn,
        check_pre_trade_filters_fn=lambda *a, **kw: filter_err,
        suggest_lot_size_fn=_default_lot,
        get_trading_balance_fn=_default_balance,
        open_trade_fn=open_trade_fn or _default_open_trade,
    ))
    return result, list(_default_open_trade.calls), bridge


def test_in_zone_execute_happy_path(fresh_db):
    result, calls, bridge = _call()
    assert result["executed"] is True
    assert len(calls) == 1
    assert calls[0]["strategy"] == "scale_out"
    assert calls[0]["stop_loss"] == 4527.0
    assert _get_last_signal()["status"] == "active"


def test_ime_followup_matched_skips_open_trade(fresh_db):
    result, calls, bridge = _call(rs={"immediate_market_entry": 1}, followup_matched=True)
    assert result["executed"] is True
    assert calls == []


def test_ime_followup_never_checked_for_limit_format_signal(fresh_db):
    # Fixed 2026-07-24: a genuine "BUY LIMITS GOLD @ .../... AREA" signal
    # (parsed["tp_open"] present, matching signal_parser.parse_limit_order_
    # signal's shape) must never be matched against an open instant-entry
    # trade as a "follow-up" -- even with IME on and even if the matcher
    # WOULD say yes (followup_matched=True here) -- it must fall through and
    # open its own new trade instead of being silently swallowed.
    limit_parsed = dict(_PARSED)
    limit_parsed["tp_open"] = False
    result, calls, bridge = _call(
        rs={"immediate_market_entry": 1}, followup_matched=True, parsed=limit_parsed,
    )
    assert len(calls) == 1
    assert result.get("followup_matched") is not True


def test_max_open_trades_reached_skips(fresh_db):
    result, calls, bridge = _call(rs={"max_open_trades": 3}, open_trades=[{}] * 3)
    assert result["executed"] is False
    assert calls == []


def test_no_tick_skips(fresh_db):
    result, calls, bridge = _call(tick=None)
    assert result["executed"] is False
    assert calls == []


def test_self_managed_conservative_uses_fixed_pre_execution_sl(fresh_db):
    result, calls, bridge = _call(strategy="conservative")
    assert result["executed"] is True
    assert calls[0]["stop_loss"] == 4526.5


def test_zone_breached_skips_outright(fresh_db):
    breached_tick = SimpleNamespace(bid=4520.0, ask=4520.5)
    result, calls, bridge = _call(tick=breached_tick)
    assert result["executed"] is False
    assert calls == []


def test_out_of_zone_not_breached_queues_pending(fresh_db):
    outside_tick = SimpleNamespace(bid=4538.0, ask=4538.5)
    result, calls, bridge = _call(tick=outside_tick)
    assert result["executed"] is False
    assert calls == []
    assert _get_last_signal()["status"] == "pending"


def test_pre_trade_filter_fails_skips(fresh_db):
    result, calls, bridge = _call(filter_err="R:R too low")
    assert result["executed"] is False
    assert calls == []


def test_validate_signal_fails_skips(fresh_db):
    bad_parsed = dict(_PARSED, tp1=4520.0)  # wrong side of entry for a BUY
    result, calls, bridge = _call(parsed=bad_parsed)
    assert result["executed"] is False
    assert calls == []


def test_open_trade_stood_down_defers_silently(fresh_db):
    async def raise_stood_down(**kwargs):
        raise ValueError("Trading stood down — the paired node has taken control")

    result, calls, bridge = _call(open_trade_fn=raise_stood_down)
    assert result["executed"] is False
    assert result.get("deferred_stood_down") is True
    assert _get_last_signal()["status"] == "pending"


def test_open_trade_circuit_breaker_surfaces_message_as_skip_reason(fresh_db):
    async def raise_cb(**kwargs):
        raise RuntimeError("circuit breaker active, cooldown 45 min")

    result, calls, bridge = _call(open_trade_fn=raise_cb)
    assert result["executed"] is False
    assert result["skip_reason"] == "circuit breaker active, cooldown 45 min"


def test_open_trade_other_error_generic_skip_reason(fresh_db):
    async def raise_other(**kwargs):
        raise RuntimeError("something unexpected broke")

    result, calls, bridge = _call(open_trade_fn=raise_other)
    assert result["executed"] is False
    assert result["skip_reason"] == "Auto-execution failed: something unexpected broke"


def test_executed_remotely_flag_passed_through(fresh_db):
    async def remote_open(**kwargs):
        return {"trade_id": "trade-remote", "entry_price": 4530.0, "executed_remotely": True}

    result, calls, bridge = _call(open_trade_fn=remote_open)
    assert result["executed"] is True
    assert result["trade_result"]["executed_remotely"] is True


def test_gap_adjusted_market_entry_gd2_source(fresh_db):
    gap_tick = SimpleNamespace(bid=4535.0, ask=4536.0)
    result, calls, bridge = _call(
        rs={"immediate_market_entry": 1}, tick=gap_tick, source_label="Gold Diggers 2.0",
    )
    assert result["executed"] is True
    assert calls[0]["stop_loss"] == 4529.0
    assert calls[0]["tp1"] == 4539.0
    assert calls[0]["entry_low"] == 4531.0
    assert "Gap-adjusted" in result["gap_note"]


def test_ea_managed_conservative_post_fill_sync(fresh_db):
    async def ea_open_trade(**kwargs):
        return {"trade_id": "trade-xyz", "entry_price": kwargs["entry_low"],
                "mt5_ticket": 999, "managed_by": "ea"}

    ea_calls = []
    fake_ea = SimpleNamespace(update_trade=mock.AsyncMock(side_effect=lambda tid, tps: ea_calls.append((tid, tps))))
    with mock.patch.object(ea_bridge, "get_instance", return_value=fake_ea):
        result, calls, bridge = _call(strategy="conservative", open_trade_fn=ea_open_trade)
    assert result["executed"] is True
    assert bridge.modify_calls == [(999, 4524.0, None)]
    assert ea_calls == [("trade-xyz", {1: 4532.0})]


# ── EA Template, grid mode: place immediately regardless of zone ─────────
# (2026-07-28) -- a grid template is a pending-order strategy by
# construction; it must never fall into the "queue and wait for price to
# re-enter the zone" path every other strategy (including single-mode
# templates) uses, or it silently places nothing until the zone happens to
# already contain price again. See core_ea_templates.py's DEFAULTS.

_OUT_OF_ZONE_TICK = SimpleNamespace(bid=4600.0, ask=4601.0)  # well above _PARSED's 4529-4534 zone


def _make_grid_template(name="Grid Tpl"):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template(name, {"mode": "grid", "grid_legs": 3})
    return et.override_for_template(name)


def _make_single_template(name="Single Tpl"):
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template(name, {"mode": "single"})
    return et.override_for_template(name)


def test_grid_template_places_immediately_when_out_of_zone(fresh_db):
    strategy = _make_grid_template()
    result, calls, bridge = _call(tick=_OUT_OF_ZONE_TICK, strategy=strategy)
    assert result["executed"] is True
    assert len(calls) == 1
    # The FULL stated zone is used, unmodified -- no gap adjustment, no
    # waiting for price to already be inside it.
    assert calls[0]["entry_low"] == 4529.0
    assert calls[0]["entry_high"] == 4534.0
    assert calls[0]["strategy"] == strategy
    sig = _get_last_signal()
    assert sig["status"] == "active"
    assert "Grid template pending order" in sig["notes"]


def test_grid_template_places_immediately_when_already_in_zone(fresh_db):
    # Behaviourally similar outcome to the plain in-zone path, but must be
    # the SAME (grid-aware) code path, not the ordinary market-fill branch --
    # distinguished here via the signal's own notes text.
    strategy = _make_grid_template()
    result, calls, bridge = _call(tick=_IN_ZONE_TICK, strategy=strategy)
    assert result["executed"] is True
    assert len(calls) == 1
    sig = _get_last_signal()
    assert "Grid template pending order" in sig["notes"]


def test_grid_template_failure_is_reported_not_swallowed(fresh_db):
    strategy = _make_grid_template()

    async def failing_open_trade(**kwargs):
        raise RuntimeError("EA rejected template order: invalid stops")

    result, calls, bridge = _call(
        tick=_OUT_OF_ZONE_TICK, strategy=strategy, open_trade_fn=failing_open_trade,
    )
    assert result["executed"] is False
    assert "Grid template order failed" in result["skip_reason"]


def test_single_mode_template_still_queues_when_out_of_zone(fresh_db):
    # Only grid mode places immediately -- single mode is a market-fill
    # strategy like any other and keeps the existing wait-then-fill path.
    strategy = _make_single_template()
    result, calls, bridge = _call(tick=_OUT_OF_ZONE_TICK, strategy=strategy)
    assert result["executed"] is False
    assert calls == []
    assert _get_last_signal()["status"] == "pending"


def test_non_template_strategy_unaffected_when_out_of_zone(fresh_db):
    result, calls, bridge = _call(tick=_OUT_OF_ZONE_TICK, strategy="scale_out")
    assert result["executed"] is False
    assert calls == []
    assert _get_last_signal()["status"] == "pending"


def test_unknown_template_name_falls_back_to_queueing(fresh_db):
    # Channel points at a template that's since been deleted/renamed --
    # must not crash, just behave like any other non-grid strategy.
    from forex_trader.core import core_ea_templates as et
    strategy = et.override_for_template("Does Not Exist")
    result, calls, bridge = _call(tick=_OUT_OF_ZONE_TICK, strategy=strategy)
    assert result["executed"] is False
    assert calls == []
