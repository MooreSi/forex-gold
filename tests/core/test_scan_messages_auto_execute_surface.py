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


def test_ea_template_bypasses_pre_trade_filter(fresh_db):
    # 2026-08-05: `strategy` here is the raw "template:<name>" override
    # string, which can never be a member of the built-in-key
    # _PRE_TRADE_FILTER_BYPASS_STRATEGIES set -- so a template channel was
    # the only execution path still scoring the signal's own TP1/SL, levels
    # the template replaces with its own sl_pips/tp*_pips before the trade
    # ever opens. Both other paths (core_signal_resolution's `not
    # _is_template`, core_pending_signal_activation's `_grid_tpl`) already
    # exempt templates. A filter verdict that WOULD block must not.
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Surface Single", {"mode": "single", "sl_pips": 60.0})
    result, calls, bridge = _call(
        strategy="template:Surface Single", filter_err="R:R too low",
    )
    assert result["executed"] is True
    assert len(calls) == 1
    assert calls[0]["strategy"] == "template:Surface Single"


def test_grid_template_reaches_immediate_placement_despite_filter(fresh_db):
    # The grid branch that stages the resting legs sits BELOW the filter
    # check, so a blocking verdict used to stop a grid template from ever
    # placing a pending order at all -- the failure that made a "BUY LIMITS
    # ... AREA" signal on a template channel place nothing.
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Surface Grid", {"mode": "grid", "pendings": 1, "anchors": 0})
    result, calls, bridge = _call(
        strategy="template:Surface Grid", filter_err="R:R too low",
    )
    assert result["executed"] is True
    assert len(calls) == 1
    # The grid branch marks the signal active immediately rather than
    # queueing it to wait for price to re-enter the zone.
    assert _get_last_signal()["status"] == "active"


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


def test_grid_template_sizes_from_lot_anchor_not_generic_risk(fresh_db):
    """Regression: this immediate-placement branch computed `lot` generically
    (risk-based, then global fixed-lot) BEFORE checking whether the
    strategy was a template at all, so a grid template's own Anchor Lot
    was never consulted here even though the EA-side grid legs already
    read tpl_lot_anchor/tpl_lot_pending directly. The value stored on the
    DB placeholder row and reported via Telegram was wrong regardless."""
    from forex_trader.core import core_ea_templates as et
    strategy = _make_grid_template("SizedGrid")
    et.save_ea_template("SizedGrid", {"mode": "grid", "grid_legs": 3,
                                      "lot_anchor": 0.05, "risk_pct": 0})
    result, calls, bridge = _call(tick=_OUT_OF_ZONE_TICK, strategy=strategy)
    assert result["executed"] is True
    assert calls[0]["lot_size"] == 0.05
    assert result["exec_lot"] == 0.05


def test_grid_template_lot_anchor_capped_by_max_lot_size(fresh_db):
    from forex_trader.core import core_ea_templates as et
    strategy = _make_grid_template("BigGrid")
    et.save_ea_template("BigGrid", {"mode": "grid", "grid_legs": 3,
                                    "lot_anchor": 5.0, "risk_pct": 0})
    result, calls, bridge = _call(
        tick=_OUT_OF_ZONE_TICK, strategy=strategy, rs={"max_lot_size": 0.10},
    )
    assert calls[0]["lot_size"] == 0.10


def test_grid_template_global_fixed_lot_does_not_override_lot_anchor(fresh_db):
    from forex_trader.core import core_ea_templates as et
    strategy = _make_grid_template("FixedLotGrid")
    et.save_ea_template("FixedLotGrid", {"mode": "grid", "grid_legs": 3,
                                         "lot_anchor": 0.03, "risk_pct": 0})
    result, calls, bridge = _call(
        tick=_OUT_OF_ZONE_TICK, strategy=strategy, rs={"strategy_lot_size": 0.77},
    )
    assert calls[0]["lot_size"] == 0.03


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


# ── IME bypasses the R:R filter (2026-08-06) ─────────────────────────────
# Immediate Market Entry means the user has opted into taking this channel's
# fill at market the moment the signal lands. An R:R gate scored against the
# live price contradicts that by construction, since IME fires wherever price
# already is rather than waiting for a better point in the zone. Live case: a
# GOLD DIGGERS INSTITUTIONAL SELL 4258-4263 (SL 4269, TP1 4256) declined at
# 0.33:1 from a live bid of 4259.30 -- inside its own zone, at the wrong end.

def test_ime_on_bypasses_pre_trade_filter(fresh_db):
    db.save_channel_parser_config("TestChannel", "auto", "", True, True, "t")
    result, calls, bridge = _call(
        rs={"immediate_market_entry": 1}, filter_err="R:R too low",
    )
    assert result["executed"] is True
    assert len(calls) == 1


def test_ime_off_still_honours_pre_trade_filter(fresh_db):
    # The per-channel flag is off, so IME is not live here and the filter
    # must still block exactly as before.
    db.save_channel_parser_config("TestChannel", "auto", "", False, True, "t")
    result, calls, bridge = _call(
        rs={"immediate_market_entry": 1}, filter_err="R:R too low",
    )
    assert result["executed"] is False
    assert calls == []


def test_global_ime_off_still_honours_pre_trade_filter(fresh_db):
    # Channel flag on, global toggle off -- IME needs both.
    db.save_channel_parser_config("TestChannel", "auto", "", True, True, "t")
    result, calls, bridge = _call(
        rs={"immediate_market_entry": 0}, filter_err="R:R too low",
    )
    assert result["executed"] is False
    assert calls == []


def test_ime_bypass_does_not_disturb_a_passing_filter(fresh_db):
    db.save_channel_parser_config("TestChannel", "auto", "", True, True, "t")
    result, calls, bridge = _call(rs={"immediate_market_entry": 1})
    assert result["executed"] is True
    assert len(calls) == 1


# ── Trading Schedule gate (2026-08-06) ───────────────────────────────────
# This path opens via core_open_trade.open_trade directly and never calls
# resolve_open_trade_params, where the schedule gate lives for every other
# route -- so a fresh Telegram signal executed regardless of the schedule
# while queued zone-fills, pending fills, IME trades and the internal
# engines were all correctly blocked. Live case: ticket 1720148940 (Gold
# Diggers VIP) opened 12:25 local against a schedule ending at 12:00.

def _sched_block(start, end, channel="TestChannel", channel_enabled=True):
    from datetime import datetime
    from forex_trader.core import core_trading_schedule as sched
    schedule = sched._default_schedule()
    day = sched.DAY_NAMES[datetime.now().weekday()]
    schedule[day][0] = {
        "enabled": True, "start": start, "end": end, "target": 0.0,
        "telegram_default_enabled": True,
        "telegram_channels": {
            db._canonical(channel): {
                "enabled": channel_enabled, "strategy_override": "",
            },
        },
    }
    sched.set_trading_schedule(schedule)
    sched.set_trading_schedule_enabled(True)


def test_outside_every_schedule_window_blocks_execution(fresh_db):
    # A window that ended before now -- the exact live shape (last window
    # closed at midday, signal arrived after).
    _sched_block("00:00", "00:01")
    result, calls, bridge = _call()
    assert result["executed"] is False
    assert calls == []
    assert "Trading Schedule" in result["skip_reason"]
    assert "outside today's trading schedule" in result["skip_reason"]


def test_inside_an_active_window_still_executes(fresh_db):
    _sched_block("00:00", "23:59")
    result, calls, bridge = _call()
    assert result["executed"] is True
    assert len(calls) == 1


def test_schedule_disabled_leaves_execution_untouched(fresh_db):
    from forex_trader.core import core_trading_schedule as sched
    _sched_block("00:00", "00:01")
    sched.set_trading_schedule_enabled(False)
    result, calls, bridge = _call()
    assert result["executed"] is True
    assert len(calls) == 1


def test_channel_disabled_in_the_active_window_blocks(fresh_db):
    # The window is open, but this specific channel is switched off in it.
    _sched_block("00:00", "23:59", channel_enabled=False)
    result, calls, bridge = _call()
    assert result["executed"] is False
    assert calls == []
    assert "Trading Schedule" in result["skip_reason"]


def test_ime_followup_still_applies_outside_the_schedule(fresh_db):
    # Deliberate ordering: a follow-up applies SL/TP to an ALREADY-OPEN
    # trade rather than opening anything. Blocking it would strand that
    # position on its provisional stop -- strictly worse than completing.
    _sched_block("00:00", "00:01")
    result, calls, bridge = _call(
        rs={"immediate_market_entry": 1}, followup_matched=True,
    )
    assert result["followup_matched"] is True
    assert result["executed"] is True
    assert calls == []


def test_schedule_block_beats_a_template_override(fresh_db):
    # Templates bypass the R:R filter, but nothing bypasses the schedule.
    from forex_trader.core import core_ea_templates as et
    et.save_ea_template("Sched Blocked", {"mode": "single", "sl_pips": 60.0})
    _sched_block("00:00", "00:01")
    result, calls, bridge = _call(strategy="template:Sched Blocked")
    assert result["executed"] is False
    assert calls == []
