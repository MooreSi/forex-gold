"""The global per-trade size reaches every automated order path.

docs/todo/risk/010. `risk/lot_sizing.py` decides the size; these tests prove
each route that used to carry its own copy of the decision now asks it:

  * the immediate Telegram path (`scan_auto_execute`), where the 2026-09-24
    0-lot orders came from;
  * the queued path (`signals/resolution`), which the Reversal, Breakout and
    Signal Generator engines all go through;
  * the Telegram limit path (`limit_order_signal`);
  * the EA grid legs (`open_trade`), which prefer the template's own leg lots
    over the lot Python sends unless told otherwise;
  * the funnel itself: `open_trade` refuses a 0-lot trade before the EA.

Each override-on assertion has an override-off twin on the same inputs, so a
path that ignored the switch fails the first and a path that ignored the
template fails the second.

Every bridge and EA here is a fake that records its arguments. Nothing here
reaches a broker, live or demo.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.services.broker import ea_templates as et
from backend.src.services.risk import lot_sizing
from backend.src.services.signals import resolution as sr
from backend.src.services.trading import open_trade as cot
from backend.src.services.trading import scan_auto_execute as ae
from tests.core.test_limit_order_template_management import (
    _place as _place_limit, _template as _limit_template,
)
from tests.core.test_scan_messages_auto_execute_surface import (
    _IN_ZONE_TICK, _OUT_OF_ZONE_TICK, _PARSED, _FakeBridge as _ScanBridge,
)
from tests.core.test_signal_resolution_surface import (
    _FakeBridge as _ResolveBridge, _insert_signal,
)

_OVERRIDE_LOTS = {"global_sizing_override": 1, "strategy_lot_size": 0.03, "max_lot_size": 0.5,
                  "risk_per_trade_pct": 2.0}
# Maximum risk per trade raised above 2%: at the schema's 1% it caps BOTH the
# single-leg 2% and the per-leg 1% to 0.10, and the grid split would pass by
# coincidence (it did, on the first run).
_OVERRIDE_RISK = {"global_sizing_override": 1, "strategy_lot_size": 0,
                  "strategy_lot_size_parked": 0.03, "max_lot_size": 1.0,
                  "risk_per_trade_pct": 2.0, "max_risk_per_trade_pct": 5.0}


def _pct_as_lot(entry, sl, balance, risk_pct):
    """A stand-in for suggest_lot_size that returns the % it was asked for,
    divided by 10 -- so the lot says which percentage was used."""
    return round(risk_pct / 10.0, 4)


# ── The immediate Telegram path ──────────────────────────────────────────────

def _immediate(strategy, rs, tick=_IN_ZONE_TICK):
    calls: list[dict] = []

    async def _open(**kw):
        calls.append(kw)
        return {"trade_id": "t1", "entry_price": 4530.0, "mt5_ticket": 1,
                "managed_by": "ea"}

    async def _no_followup(*a):
        return False

    async def _balance():
        return 1000.0

    result = asyncio.run(ae.execute_auto_signal(
        dict(_PARSED), "tg1", "Chan", "Chan", strategy, rs, True, False,
        "", "", _ScanBridge(tick),
        get_open_trades_fn=lambda: [],
        find_and_apply_instant_followup_fn=_no_followup,
        check_pre_trade_filters_fn=lambda *a, **kw: None,
        suggest_lot_size_fn=_pct_as_lot,
        get_trading_balance_fn=_balance,
        open_trade_fn=_open,
    ))
    return result, calls


class TestTheImmediatePath:
    def _single(self, fresh_db):
        et.save_ea_template("Single", {"mode": "single", "lot_anchor": 0.1, "risk_pct": 0})
        return et.override_for_template("Single")

    def test_override_off_keeps_the_templates_anchor_lot(self, fresh_db):
        _, calls = _immediate(self._single(fresh_db), {"max_lot_size": 0.5})
        assert calls[0]["lot_size"] == 0.1

    def test_override_on_fixed_lots_replaces_it(self, fresh_db):
        _, calls = _immediate(self._single(fresh_db), dict(_OVERRIDE_LOTS))
        assert calls[0]["lot_size"] == 0.03

    def test_override_on_risk_splits_the_percentage_across_grid_legs(self, fresh_db):
        et.save_ea_template("Grid", {"mode": "grid", "anchors": 1, "pendings": 1,
                                     "lot_anchor": 0.04, "risk_pct": 0})
        _, calls = _immediate(et.override_for_template("Grid"), dict(_OVERRIDE_RISK),
                              tick=_OUT_OF_ZONE_TICK)
        assert calls[0]["lot_size"] == 0.1        # 2% / 2 legs

    def test_a_plain_channel_in_risk_mode_ignores_a_parked_fixed_lot(self, fresh_db):
        rs = {"strategy_lot_size": 0, "strategy_lot_size_parked": 0.1,
              "risk_per_trade_pct": 2.0, "max_lot_size": 1.0}
        _, calls = _immediate("scale_out", rs)
        assert calls[0]["lot_size"] == 0.2

    def test_a_plain_channel_in_lots_mode_uses_it(self, fresh_db):
        rs = {"strategy_lot_size": 0.1, "risk_per_trade_pct": 2.0, "max_lot_size": 1.0}
        _, calls = _immediate("scale_out", rs)
        assert calls[0]["lot_size"] == 0.1


# ── The queued path (engines and queued Telegram signals) ───────────────────

class TestTheQueuedPath:
    def _tpl_channel(self, template: dict):
        et.save_ea_template("QTpl", template)
        db.set_channel_strategy_override("QChan", et.override_for_template("QTpl"))

    def _resolve(self, rs: dict, **sig):
        db.update_risk_settings(rs)
        _insert_signal(source_name="QChan", stop_loss=2390.0, **sig)
        bridge = _ResolveBridge(account={"balance": 10000.0})
        return asyncio.run(sr.resolve_open_trade_params(bridge, "sig-1"))["lot_size"]

    def test_override_off_keeps_the_templates_anchor_lot(self, fresh_db):
        self._tpl_channel({"lot_anchor": 0.05, "risk_pct": 0})
        assert self._resolve({"max_lot_size": 0.5}) == 0.05

    def test_override_on_fixed_lots_replaces_it(self, fresh_db):
        self._tpl_channel({"lot_anchor": 0.05, "risk_pct": 0})
        assert self._resolve(_OVERRIDE_LOTS) == 0.03

    def test_override_on_risk_is_the_total_for_a_grid(self, fresh_db):
        # entry ~2400, SL 2390: 10pt. 2% of 10000 = $200 over 2 legs = $100
        # a leg = 0.10 lots a leg.
        self._tpl_channel({"mode": "grid", "anchors": 1, "pendings": 1,
                           "lot_anchor": 0.05, "risk_pct": 0})
        assert self._resolve(_OVERRIDE_RISK) == 0.10

    def test_a_single_leg_template_takes_the_whole_percentage(self, fresh_db):
        self._tpl_channel({"mode": "single", "lot_anchor": 0.05, "risk_pct": 0})
        assert self._resolve(_OVERRIDE_RISK) == 0.20

    def test_an_engine_supplied_lot_is_resized_when_the_override_is_on(self, fresh_db):
        # The Breakout Engine stores its own ML/Kelly-scaled lot on the signal.
        self._tpl_channel({"lot_anchor": 0.05, "risk_pct": 0})
        assert self._resolve(_OVERRIDE_LOTS, lot_size=0.07) == 0.03

    def test_and_kept_when_it_is_off(self, fresh_db):
        self._tpl_channel({"lot_anchor": 0.05, "risk_pct": 0})
        assert self._resolve({"max_lot_size": 0.5}, lot_size=0.07) == 0.07

    def test_a_lot_a_person_typed_still_wins(self, fresh_db):
        self._tpl_channel({"lot_anchor": 0.05, "risk_pct": 0})
        db.update_risk_settings(_OVERRIDE_LOTS)
        _insert_signal(source_name="QChan")
        result = asyncio.run(sr.resolve_open_trade_params(
            _ResolveBridge(), "sig-1", lot_size_override=0.09))
        assert result["lot_size"] == 0.09


# ── The Telegram limit path ─────────────────────────────────────────────────

class TestTheLimitPath:
    @pytest.mark.asyncio
    async def test_override_off_keeps_the_templates_anchor_lot(self, fresh_db):
        ea = await _place_limit(fresh_db, _limit_template(lot_anchor=0.02),
                                rs={"max_lot_size": 0.10, "lk_entry_realignment": 0})
        assert ea.calls[0]["lot_size"] == pytest.approx(0.02)

    @pytest.mark.asyncio
    async def test_override_on_fixed_lots_replaces_it(self, fresh_db):
        ea = await _place_limit(fresh_db, _limit_template(lot_anchor=0.02),
                                rs=dict(_OVERRIDE_LOTS, lk_entry_realignment=0))
        assert ea.calls[0]["lot_size"] == pytest.approx(0.03)

    @pytest.mark.asyncio
    async def test_a_zero_lot_is_refused_before_the_ea(self, fresh_db):
        ea = await _place_limit(fresh_db, _limit_template(lot_anchor=0.02),
                                rs={"max_lot_size": 0.0, "lk_entry_realignment": 0})
        assert ea.calls == []


# ── The EA grid legs and the funnel ─────────────────────────────────────────

class _RecordingEA:
    def __init__(self):
        self.calls: list[dict] = []

    def is_ea_healthy(self):
        return True

    def is_strategy_portable(self, strategy):
        return True

    async def open_trade(self, trade_id, direction, lot_size, stop_loss, tps,
                         strategy, **kw):
        self.calls.append(dict(lot_size=lot_size, template=kw.get("template")))
        return {"type": "trade_opened", "ticket": 0, "fill_price": 0.0,
                "legs_placed": 2}


class _Tick:
    ask = 4064.2
    bid = 4064.0
    spread_points = 20.0


class _GridBridge:
    async def get_fresh_tick(self):
        return _Tick()


def _open_grid(monkeypatch, rs: dict, lot: float):
    et.save_ea_template("Grid", {"mode": "grid", "anchors": 1, "pendings": 1,
                                 "lot_anchor": 0.04, "lot_pending": 0.04,
                                 "tp1_pips": 20.0, "tp1_pct": 100.0})
    db.update_risk_settings({"ea_bridge_enabled": 1, "max_open_trades": 10, **rs})
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,status,created_at) VALUES (?,?,?,?,?,?,?,?)",
            ("sig-g", "Reversal Engine", "SELL", 4063.0, 4066.0, 4071.5,
             "pending", time.time()))
    ea = _RecordingEA()
    from backend.src.services.broker import ea_bridge as ea_mod
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
    asyncio.run(cot.open_trade(
        _GridBridge(), "sig-g", "SELL", 4063.0, 4066.0, 4071.5,
        tp1=4060.0, lot_size=lot, tick=_Tick(),
        strategy=et.override_for_template("Grid"), tg_source="Reversal Engine",
    ))
    return ea


class TestTheGridLegs:
    def test_override_off_leaves_the_templates_own_leg_lots(self, monkeypatch, fresh_db):
        ea = _open_grid(monkeypatch, {"max_lot_size": 0.5}, 0.04)
        sent = ea.calls[0]["template"]
        assert (sent["lot_anchor"], sent["lot_pending"]) == (0.04, 0.04)

    def test_override_on_sends_the_computed_lot_for_every_leg(self, monkeypatch, fresh_db):
        # The EA prefers a non-zero template leg lot over the lot it is sent,
        # so without this the override would do nothing on a grid.
        ea = _open_grid(monkeypatch, _OVERRIDE_LOTS, 0.03)
        sent = ea.calls[0]["template"]
        assert (sent["lot_anchor"], sent["lot_pending"]) == (0.03, 0.03)
        assert ea.calls[0]["lot_size"] == 0.03

    def test_the_stored_template_is_not_rewritten(self, monkeypatch, fresh_db):
        _open_grid(monkeypatch, _OVERRIDE_LOTS, 0.03)
        assert et.get_ea_template("Grid")["lot_anchor"] == 0.04


class _NothingBridge:
    """Records any use at all. A refused trade must not touch the bridge."""

    def __init__(self):
        self.used: list[str] = []

    def __getattr__(self, name):
        self.used.append(name)
        raise AssertionError(f"bridge.{name} used by a refused trade")


class TestTheFunnelRefusesZeroLots:
    def test_open_trade_refuses_a_zero_lot_before_anything_else(self, fresh_db):
        db.update_risk_settings({"max_lot_size": 0.0})
        bridge = _NothingBridge()
        with pytest.raises(lot_sizing.UnplaceableLot, match="Maximum lot size"):
            asyncio.run(cot.open_trade(
                bridge, "sig-z", "BUY", 4268.0, 4272.0, 4263.0,
                lot_size=0.0, tick=_Tick(), strategy="scale_out"))
        assert bridge.used == []

    def test_the_incident_now_reports_why(self, fresh_db):
        """2026-09-24, Gold Diggers VIP: template anchor 0.1, Maximum lot size
        0, override off. The immediate path must hand open_trade a lot the
        funnel then refuses by name, not a bare MT5 'invalid volume'."""
        et.save_ea_template("30 TP1", {"mode": "single", "lot_anchor": 0.1, "risk_pct": 0})
        rs = {"max_lot_size": 0.0, "risk_per_trade_pct": 2.0}
        db.update_risk_settings(rs)
        bridge = _NothingBridge()

        async def _real_funnel(**kw):
            return await cot.open_trade(bridge, **kw)

        async def _no_followup(*a):
            return False

        async def _balance():
            return 1000.0

        result = asyncio.run(ae.execute_auto_signal(
            dict(_PARSED), "tg1", "Gold Diggers VIP", "Gold Diggers VIP",
            et.override_for_template("30 TP1"), rs, True, False, "", "",
            _ScanBridge(_IN_ZONE_TICK),
            get_open_trades_fn=lambda: [],
            find_and_apply_instant_followup_fn=_no_followup,
            check_pre_trade_filters_fn=lambda *a, **kw: None,
            suggest_lot_size_fn=_pct_as_lot,
            get_trading_balance_fn=_balance,
            open_trade_fn=_real_funnel,
        ))
        assert result["executed"] is False
        assert "Maximum lot size" in result["skip_reason"]
        assert bridge.used == []


# ── Immediate Market Entry (the bare "Buy now" message) ─────────────────────

class TestTheImePath:
    def _run(self, rs_over: dict):
        from unittest import mock
        from tests.core.test_instant_entry_surface import _rs, _run_open_trade_path
        et.save_ea_template("ImeTpl", {"lot_anchor": 0.06, "risk_pct": 0, "sl_pips": 50})
        with mock.patch.object(db, "get_channel_strategy_override",
                               return_value=et.override_for_template("ImeTpl")):
            ot = _run_open_trade_path(dict(_rs(), **rs_over), tg_id="tg-ovr")
        assert ot.called
        return ot.call_args.kwargs["lot_size"]

    def test_override_off_keeps_the_templates_anchor_lot(self, fresh_db):
        assert self._run({"max_lot_size": 0.5}) == 0.06

    def test_override_on_fixed_lots_replaces_it(self, fresh_db):
        assert self._run(_OVERRIDE_LOTS) == 0.03
