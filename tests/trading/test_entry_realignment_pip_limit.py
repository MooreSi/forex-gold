"""Entry Realignment's pip limit: one cap, every entry route.

Live 2026-10-02, Gold Diggers VIP on the "Test" EA template, Entry
Realignment ON: "BUY ENTRY 4180-4176 SL 4174" arrived with the ask at
4183.80. The signal was queued, because realignment only ever handled the
OTHER side on this route -- price falling through a BUY zone toward its stop.
Price having run away above a BUY zone was realigned only on the Limit Runner
route (hence the box's "Limit Runner only"), and by any distance at all.

Owner, 2026-10-02:

  * a "realign up to N pips" field in the Entry Realignment box;
  * one limit, both ways: price that MISSED the zone (BUY above / SELL below)
    and price that went THROUGH it toward the stop are both realigned within
    N pips. Missed by more: wait for the zone. Breached by more: discarded,
    as before;
  * every route: market strategies, single and grid EA templates, Limit
    Runner, and the watcher that re-checks queued signals;
  * grid templates shift the whole zone so the first leg sits at market;
  * when a limit is set it is the only cap: Immediate Market Entry's fixed
    15-point gap-fire applies only when no limit is set.

**Blank (0) changes nothing.** Every route behaves exactly as it did before
the field existed: the missed side queues on the market route, Limit Runner
realigns by any distance, a breach realigns by any distance. Golden rule 3.

1 pip = 0.10 here (core_pips.PIPS_TO_PRICE_XAUUSD), so the live 4183.80
against a 4180 zone top is 38 pips.

Nothing here reaches a broker. Order calls are recording sentinels, the EA is
the `_FakeEA` double from test_limit_order_signal.py, and bridges return
canned ticks.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace
from unittest import mock
from unittest.mock import patch

import pytest

from backend.src.db import database as db
from backend.src.services.signals import pending_activation as psa
from backend.src.services.trading import entry_realignment as er
from backend.src.services.trading import limit_order_signal as los
from backend.src.services.trading import scan_auto_execute as ae
from tests._fakes import _FakeBridge
from tests.core.test_limit_order_signal import (
    _FakeBridge as _TickBridge, _FakeEA, _balance, _insert_tg_row, _lot_size, _parsed,
)

ON = {"lk_entry_realignment": 1}


def _rs(pips, **extra):
    return {**ON, "lk_entry_realignment_max_pips": pips, **extra}


# ── the rule, as pure arithmetic ────────────────────────────────────────────

class TestTheLimit:
    def test_pips_convert_at_ten_to_the_point(self):
        assert er.realign_cap_pts(_rs(38)) == pytest.approx(3.8)

    def test_no_limit_when_realignment_is_off(self):
        assert er.realign_cap_pts({"lk_entry_realignment": 0,
                                   "lk_entry_realignment_max_pips": 38}) == 0.0

    def test_blank_is_no_limit(self):
        assert er.realign_cap_pts(_rs(0)) == 0.0
        assert er.realign_cap_pts(ON) == 0.0
        assert er.realign_cap_pts(_rs(None)) == 0.0

    def test_a_set_limit_overrides_the_ime_gap_fire(self):
        assert er.gap_fire_cap_pts(_rs(20), ime_on=True, ime_cap=15.0) == pytest.approx(2.0)

    def test_without_a_limit_ime_keeps_its_own_cap(self):
        assert er.gap_fire_cap_pts(_rs(0), ime_on=True, ime_cap=15.0) == 15.0

    def test_without_a_limit_or_ime_nothing_fires(self):
        assert er.gap_fire_cap_pts(_rs(0), ime_on=False, ime_cap=15.0) == 0.0

    def test_a_limit_applies_with_ime_off(self):
        assert er.gap_fire_cap_pts(_rs(38), ime_on=False, ime_cap=15.0) == pytest.approx(3.8)


_LIVE = {"direction": "BUY", "entry_low": 4176.0, "entry_high": 4180.0, "stop_loss": 4174.0,
         "tp1": 4182.0, "tp2": 4183.0, "tp3": 4184.0, "tp4": 4185.0, "tp5": 4186.0,
         "tp6": 4190.0, "tp7": 4194.0, "tp8": None}


class TestTheMissedSide:
    def test_the_live_case_shifts_everything_by_the_gap(self):
        out = er.realign_missed(dict(_LIVE), 4183.8, 3.8)

        assert out is not None
        shifted, gap = out
        assert gap == pytest.approx(3.8)
        assert shifted["entry_low"] == pytest.approx(4179.8)
        assert shifted["entry_high"] == pytest.approx(4183.8)
        assert shifted["stop_loss"] == pytest.approx(4177.8)
        assert shifted["tp1"] == pytest.approx(4185.8)
        assert shifted["tp7"] == pytest.approx(4197.8)
        assert shifted["tp8"] is None

    def test_one_tick_past_the_limit_is_not_realigned(self):
        assert er.realign_missed(dict(_LIVE), 4183.9, 3.8) is None

    def test_in_the_zone_is_not_realigned(self):
        assert er.realign_missed(dict(_LIVE), 4179.0, 3.8) is None

    def test_no_limit_never_realigns(self):
        assert er.realign_missed(dict(_LIVE), 4180.5, 0.0) is None

    def test_sell_below_its_zone(self):
        sell = {"direction": "SELL", "entry_low": 4190.0, "entry_high": 4194.0,
                "stop_loss": 4197.0, "tp1": 4186.0}
        shifted, gap = er.realign_missed(sell, 4188.0, 3.0)

        assert gap == pytest.approx(2.0)
        assert shifted["entry_low"] == pytest.approx(4188.0)
        assert shifted["stop_loss"] == pytest.approx(4195.0)
        assert shifted["tp1"] == pytest.approx(4184.0)

    def test_the_input_is_not_mutated(self):
        p = dict(_LIVE)
        er.realign_missed(p, 4183.8, 3.8)
        assert p == _LIVE


class TestTheBreachSide:
    def _breach(self, live_px, max_pts):
        return er.realign_for_breach(
            direction="BUY", entry_low=4176.0, entry_high=4180.0, live_px=live_px,
            stop_loss=4170.0, tps={1: 4182.0}, max_pts=max_pts)

    def test_within_the_limit_is_realigned(self):
        assert self._breach(4174.0, 3.0).stop_loss == pytest.approx(4168.0)

    def test_beyond_the_limit_is_not(self):
        assert self._breach(4172.0, 3.0) is None

    def test_no_limit_keeps_todays_uncapped_behaviour(self):
        assert self._breach(4166.0, 0.0).stop_loss == pytest.approx(4160.0)


# ── the market route: scan_auto_execute ─────────────────────────────────────

_GD = {"direction": "BUY", "entry_low": 4529.0, "entry_high": 4534.0, "stop_loss": 4527.0,
       "tp1": 4537.0, "tp2": 4539.0, "tp3": 4541.0, "tp4": 4543.0, "tp5": 4545.0,
       "tp6": None, "tp7": None, "tp8": None}
_ABOVE = SimpleNamespace(bid=4538.3, ask=4538.5)    # 4.5 past the zone top = 45 pips
_BELOW = SimpleNamespace(bid=4525.5, ask=4525.7)    # 3.3 under the zone floor = 33 pips


async def _open_sentinel(**kwargs):
    _open_sentinel.calls.append(kwargs)
    return {"trade_id": "t", "entry_price": kwargs.get("entry_high"), "mt5_ticket": 1,
            "managed_by": "ea"}
_open_sentinel.calls = []


def _execute(rs, tick, strategy="scale_out"):
    _open_sentinel.calls = []

    async def _no_followup(*a):
        return False

    async def _bal():
        return 1000.0

    result = asyncio.run(ae.execute_auto_signal(
        dict(_GD), "tg1", "TestChannel", "TestChannel", strategy, rs,
        True, False, "", "", _FakeBridge(tick=tick),
        get_open_trades_fn=lambda: [],
        find_and_apply_instant_followup_fn=_no_followup,
        check_pre_trade_filters_fn=lambda *a, **k: None,
        suggest_lot_size_fn=lambda *a: 0.01,
        get_trading_balance_fn=_bal,
        open_trade_fn=_open_sentinel,
    ))
    return result, list(_open_sentinel.calls)


def _last_status():
    with db.db() as conn:
        r = conn.execute("SELECT status FROM vantage_signals ORDER BY created_at DESC LIMIT 1").fetchone()
    return r[0] if r else None


def _template(mode):
    from backend.src.services.broker import ea_templates as et
    et.save_ea_template(f"T-{mode}", {"mode": mode})
    return et.override_for_template(f"T-{mode}")


class TestTheMarketRoute:
    def test_missed_within_the_limit_enters_at_market_with_shifted_levels(self, fresh_db):
        db.save_channel_parser_config("TestChannel", "gd2", "", True, True, "t")
        result, calls = _execute(_rs(50), _ABOVE)

        assert result["executed"] is True
        assert len(calls) == 1
        assert calls[0]["entry_high"] == pytest.approx(4538.5)
        assert calls[0]["stop_loss"] == pytest.approx(4531.5)
        assert calls[0]["tp1"] == pytest.approx(4541.5)

    def test_missed_beyond_the_limit_waits_for_the_zone(self, fresh_db):
        result, calls = _execute(_rs(40), _ABOVE)

        assert calls == []
        assert _last_status() == "pending"

    def test_blank_limit_waits_exactly_as_before(self, fresh_db):
        result, calls = _execute(_rs(0), _ABOVE)

        assert calls == []
        assert _last_status() == "pending"

    def test_the_limit_beats_imes_wider_gap_fire(self, fresh_db):
        """IME alone would fire at 4.5 (its cap is 15). A 40-pip limit says no."""
        db.save_channel_parser_config("TestChannel", "gd2", "", True, True, "t")
        result, calls = _execute(_rs(40, immediate_market_entry=1), _ABOVE)

        assert calls == []
        assert _last_status() == "pending"

    def test_ime_without_a_limit_still_gap_fires(self, fresh_db):
        """Control: blank limit leaves IME's own 15-point gap-fire alone."""
        db.save_channel_parser_config("TestChannel", "gd2", "", True, True, "t")
        result, calls = _execute(_rs(0, immediate_market_entry=1), _ABOVE)

        assert len(calls) == 1
        assert calls[0]["entry_high"] == pytest.approx(4538.5)

    def test_breach_within_the_limit_is_realigned(self, fresh_db):
        result, calls = _execute(_rs(40), _BELOW)

        assert len(calls) == 1
        assert calls[0]["stop_loss"] == pytest.approx(4523.7)

    def test_breach_beyond_the_limit_is_discarded(self, fresh_db):
        result, calls = _execute(_rs(30), _BELOW)

        assert calls == []
        assert "breached" in result["skip_reason"]


class TestEaTemplates:
    def test_a_single_template_is_realigned(self, fresh_db):
        strategy = _template("single")
        result, calls = _execute(_rs(50), _ABOVE, strategy=strategy)

        assert len(calls) == 1
        assert calls[0]["strategy"] == strategy
        assert calls[0]["stop_loss"] == pytest.approx(4531.5)

    def test_a_single_template_beyond_the_limit_waits(self, fresh_db):
        result, calls = _execute(_rs(40), _ABOVE, strategy=_template("single"))

        assert calls == []
        assert _last_status() == "pending"

    def test_a_grid_shifts_its_whole_zone_to_market(self, fresh_db):
        strategy = _template("grid")
        result, calls = _execute(_rs(50), _ABOVE, strategy=strategy)

        assert len(calls) == 1
        assert calls[0]["entry_low"] == pytest.approx(4533.5)
        assert calls[0]["entry_high"] == pytest.approx(4538.5)
        assert calls[0]["stop_loss"] == pytest.approx(4531.5)

    def test_a_grid_beyond_the_limit_rests_at_the_signalled_zone(self, fresh_db):
        result, calls = _execute(_rs(40), _ABOVE, strategy=_template("grid"))

        assert calls[0]["entry_low"] == 4529.0
        assert calls[0]["entry_high"] == 4534.0

    def test_a_grid_with_no_limit_is_unchanged(self, fresh_db):
        """IME has never shifted a grid; a blank limit must not start now."""
        db.save_channel_parser_config("TestChannel", "gd2", "", True, True, "t")
        result, calls = _execute(_rs(0, immediate_market_entry=1), _ABOVE,
                                 strategy=_template("grid"))

        assert calls[0]["entry_high"] == 4534.0


# ── the watcher: a signal queued beyond the limit, price comes closer ───────

def _queued(sig_id="sig-1"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, tp1, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sig_id, "Chan", "BUY", 2399.0, 2401.0, 2390.0, 2410.0, "pending", time.time()))


def _watch(rs, tick):
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={"entry_price": 0, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(
            tick, {"max_open_trades": 1, "trade_strategy": "scale_out", **rs},
            _FakeBridge(), {}, []))
    return ot


_NEAR = SimpleNamespace(bid=2402.8, ask=2403.0)    # 2.0 past the 2401 top = 20 pips


class TestTheWatcher:
    def test_within_the_limit_it_fires_without_ime(self, fresh_db):
        _queued()

        assert _watch(_rs(25), _NEAR).called

    def test_beyond_the_limit_it_keeps_waiting(self, fresh_db):
        _queued()

        assert not _watch(_rs(15), _NEAR).called

    def test_blank_limit_without_ime_keeps_waiting_as_before(self, fresh_db):
        _queued()

        assert not _watch(_rs(0), _NEAR).called

    def test_the_limit_beats_imes_cap_here_too(self, fresh_db):
        _queued()
        db.save_channel_parser_config("Chan", "gd2", "", True, True, "")

        assert not _watch(_rs(15, immediate_market_entry=1), _NEAR).called


# ── Limit Runner ────────────────────────────────────────────────────────────

def _limit(rs, ask):
    async def go():
        await _insert_tg_row("tg1")
        ea = _FakeEA(open_trade_ack={"type": "trade_opened", "ticket": 777, "fill_price": ask})
        with patch("backend.src.services.broker.ea_bridge.get_instance", return_value=ea):
            await los.handle_limit_order_signal(
                _parsed("BUY", tp_open=False), "tg1", "chan", "chan",
                {"risk_per_trade_pct": 0.5, "strategy_lot_size": 0, **rs},
                sess_ok=True, per_signal_skip=False, per_signal_skip_reason="",
                skip_reason="", get_trading_balance_fn=_balance,
                suggest_lot_size_fn=_lot_size, bridge=_TickBridge(bid=ask - 0.2, ask=ask))
        return ea
    return asyncio.run(go())


class TestLimitRunner:
    """Limit price is the 4148 zone top for a BUY (see _parsed)."""

    def test_within_the_limit_enters_at_market(self, fresh_db):
        ea = _limit(_rs(30), 4150.0)

        assert len(ea.open_trade_calls) == 1
        assert ea.calls == []

    def test_beyond_the_limit_rests_a_limit_order_instead(self, fresh_db):
        ea = _limit(_rs(10), 4150.0)

        assert ea.open_trade_calls == []
        assert len(ea.calls) == 1

    def test_blank_limit_keeps_the_uncapped_realignment(self, fresh_db):
        ea = _limit(_rs(0), 4170.0)

        assert len(ea.open_trade_calls) == 1
