"""A template's "use dynamic ATR" sets the STOP, not only the targets
(handover 031, measured 2026-10-07).

`template_sl_at` read its ATR from the DPM M5 cache, which `monitor_cycle`
fills only when `dpm_enabled` -- 0 on this account -- so the stop silently
fell back to the fixed `sl_pips`. The Reversal Engine and limit-order routes
passed no candles at all. Meanwhile `open_trade` fetched M5 candles from the
bridge for the TARGETS, so a dynamic-ATR template would have sent ATR-scaled
targets around a fixed stop, and sized the lot from the fixed one.

Now one helper, `template_levels.template_atr`, reads M5 from the bridge
(cached briefly, so a trade's stop and targets use the same figure), and
every route passes that ATR to `template_sl_at`.

No broker is involved: the bridge is a fake that serves candles.
"""
import asyncio

import pytest

from backend.src.services.trading import template_levels as tl

DYN = {"use_dynamic_atr": True, "atr_period": 14, "atr_sl_mult": 2.0, "sl_pips": 50.0}
FIXED = {"use_dynamic_atr": False, "sl_pips": 50.0}


def _candles(rng=3.0, n=30):
    # Each bar spans `rng` and opens where the last closed: ATR == rng.
    return [{"ts": i * 300, "open": 4100.0, "high": 4100.0 + rng / 2,
             "low": 4100.0 - rng / 2, "close": 4100.0} for i in range(n)]


class _Bridge:
    def __init__(self, candles=None, fail=False):
        self.calls, self._c, self._fail = [], candles, fail

    async def get_candles(self, tf, count):
        self.calls.append((tf, count))
        if self._fail:
            raise ConnectionError("bridge down")
        return self._c


@pytest.fixture(autouse=True)
def _fresh_cache():
    tl.reset_atr_cache()
    yield
    tl.reset_atr_cache()


def test_the_atr_comes_from_the_bridge_m5():
    b = _Bridge(_candles(3.0))
    atr = asyncio.run(tl.template_atr(DYN, b))
    assert atr == pytest.approx(3.0)
    assert b.calls and b.calls[0][0] == "M5"


def test_a_fixed_template_asks_the_bridge_nothing():
    b = _Bridge(_candles(3.0))
    assert asyncio.run(tl.template_atr(FIXED, b)) is None
    assert b.calls == []


def test_a_failed_read_is_none_not_an_error():
    assert asyncio.run(tl.template_atr(DYN, _Bridge(fail=True))) is None
    assert asyncio.run(tl.template_atr(DYN, None)) is None


def test_two_reads_close_together_give_the_same_figure_from_one_call():
    b = _Bridge(_candles(3.0))
    a1 = asyncio.run(tl.template_atr(DYN, b))
    a2 = asyncio.run(tl.template_atr(DYN, b))
    assert a1 == a2 and len(b.calls) == 1


def test_the_stop_follows_the_atr_without_any_dpm_candles():
    sl = tl.template_sl_at(DYN, "BUY", 4100.0, None, atr=3.0)
    assert sl == pytest.approx(4100.0 - 6.0)          # 2 x ATR, not 5.0


def test_without_an_atr_the_stop_is_the_fixed_one_as_before():
    assert tl.template_sl_at(DYN, "SELL", 4100.0, None) == pytest.approx(4105.0)
    assert tl.template_sl_at(FIXED, "SELL", 4100.0, None, atr=3.0) == pytest.approx(4105.0)


# ── The queued path (resolution.py): stop AND lot from the ATR ──────────────

from backend.src.db import database as db                       # noqa: E402
from backend.src.services.broker import ea_templates as et      # noqa: E402
from backend.src.services.signals import resolution as sr      # noqa: E402
from tests.core.test_signal_resolution_surface import (         # noqa: E402
    _FakeBridge as _ResolveBridge, _insert_signal,
)
from tests.risk.test_template_trade_sized_from_the_stop_it_places import _RISK_2PCT  # noqa: E402


class _CandleBridge(_ResolveBridge):
    async def get_candles(self, tf, count):
        return _candles(4.0)                                     # ATR 4.0


def _resolve_dyn(fresh_db, bridge_cls=_CandleBridge):
    et.save_ea_template("DYN", {"mode": "single", "risk_pct": 0, "sl_pips": 50,
                                "use_dynamic_atr": True, "atr_period": 14, "atr_sl_mult": 2.0})
    db.set_channel_strategy_override("QChan", et.override_for_template("DYN"))
    db.update_risk_settings(_RISK_2PCT)
    _insert_signal(source_name="QChan", stop_loss=2398.0)
    return asyncio.run(sr.resolve_open_trade_params(
        bridge_cls(account={"balance": 10000.0}), "sig-1"))


def test_the_queued_path_places_the_atr_stop(fresh_db):
    r = _resolve_dyn(fresh_db)
    assert r["stop_loss_to_use"] == pytest.approx(2400.2 - 8.0)   # 2 x ATR 4, not 5


def test_the_queued_path_sizes_the_lot_from_that_stop(fresh_db):
    r = _resolve_dyn(fresh_db)
    assert r["lot_size"] == 0.25                                  # $200 over 8 points


def test_with_no_candles_the_queued_path_keeps_the_fixed_stop(fresh_db):
    r = _resolve_dyn(fresh_db, bridge_cls=_ResolveBridge)        # no get_candles at all
    assert r["stop_loss_to_use"] == pytest.approx(2400.2 - 5.0)
    assert r["lot_size"] == 0.40


# ── The Reversal Engine's limit orders and the Limit Runner ─────────────────

from backend.src.services.broker import ea_bridge as ea_mod                           # noqa: E402
from backend.src.services.reversal_engine import reversal_engine_repo as re_db        # noqa: E402
from backend.src.services.reversal_engine.reversal_engine_live_execute import _LiveExecuteMixin  # noqa: E402
from backend.src.services.trading import close_trade                                   # noqa: E402
from backend.src.services.trading import limit_order_signal as los                     # noqa: E402
from tests.trading.test_re_limit_order_sends_the_template import _RecordingEA          # noqa: E402

_DYN_TPL = {"mode": "single", "anchors": 1, "pendings": 0, "sl_pips": 50.0,
            "tp1_pips": 40.0, "tp1_pct": 100.0, "risk_pct": 1.0,
            "use_dynamic_atr": True, "atr_period": 14, "atr_sl_mult": 2.0}


def test_a_reversal_engine_limit_order_rests_with_the_atr_stop(fresh_db, monkeypatch):
    et.save_ea_template("DYN", _DYN_TPL)
    ea = _RecordingEA()
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
    monkeypatch.setattr(re_db, "claim_vantage_signal_activation", lambda _sid: 1)
    monkeypatch.setattr(re_db, "insert_vantage_pending_order", lambda row: None)
    monkeypatch.setattr(re_db, "update_live_exec", lambda *a, **k: None)
    monkeypatch.setattr(re_db, "restore_vantage_signal_pending", lambda *_a: None)

    async def _balance(_b, _s):
        return 10_000.0
    monkeypatch.setattr(close_trade, "get_trading_balance", _balance)

    async def _resolved(_bridge, _sid, tick=None):
        return {"strategy": et.override_for_template("DYN"), "lot_size": 0.02,
                "stop_loss_to_use": 4131.72, "tick": None,
                "sig": {"direction": "BUY", "entry_low": 4139.0, "entry_high": 4142.0,
                        "tp1": 4146.0}}
    monkeypatch.setattr(sr, "resolve_open_trade_params", _resolved)
    mixin = _LiveExecuteMixin()
    mixin._bridge = _CandleBridge()
    assert asyncio.run(mixin._try_re_limit_order({"id": 1, "signal_ref": "RE-1"}, "v1", None))
    stop = ea.calls[0][0][4]
    assert stop == pytest.approx(4142.0 - 8.0)                    # 2 x ATR 4 from the resting price


def test_a_limit_runner_order_rests_with_the_atr_stop(fresh_db, monkeypatch):
    et.save_ea_template("DYN", _DYN_TPL)
    db.set_channel_strategy_override("QChan", et.override_for_template("DYN"))
    ea = _RecordingEA()
    monkeypatch.setattr(ea_mod, "get_instance", lambda: ea)
    with db.db() as conn:
        conn.execute("INSERT INTO vantage_tg_signals (tg_message_id,group_id,raw_text,parsed_at,status) "
                     "VALUES (?,?,?,?,?)", ("9", "g", "raw", 0.0, "new"))

    async def _bal():
        return 10_000.0
    parsed = {"direction": "BUY", "entry_low": 4139.0, "entry_high": 4142.0,
              "stop_loss": 4130.0, "tp1": 4146.0, "tp_open": False}
    asyncio.run(los.handle_limit_order_signal(
        parsed, "9", "QChan", "QChan", {"risk_per_trade_pct": 1.0, "strategy_lot_size": 0},
        sess_ok=True, per_signal_skip=False, per_signal_skip_reason="", skip_reason="",
        get_trading_balance_fn=_bal, suggest_lot_size_fn=lambda e, s, b, r: 0.02,
        bridge=_CandleBridge()))
    assert ea.calls, "a resting order"
    stop = ea.calls[0][0][4]
    assert stop == pytest.approx(4142.0 - 8.0)


# ── Instant entry: its own copy of the rule, same gap ───────────────────────

from unittest import mock                                                    # noqa: E402
from backend.src.services.trading import instant_entry as ime                # noqa: E402
from tests.core import test_instant_entry_surface as ies                     # noqa: E402


class _CandleIMEBridge(ies._FakeBridge):
    async def get_candles(self, tf, count):
        return _candles(4.0)


def test_instant_entry_takes_the_atr_stop_with_no_dpm_candles(fresh_db):
    et.save_ea_template("AtrTpl", {"lot_anchor": 0.05, "risk_pct": 0, "sl_pips": 50,
                                   "use_dynamic_atr": 1, "atr_sl_mult": 1.5})
    with mock.patch.object(db, "get_channel_strategy_override",
                           return_value=et.override_for_template("AtrTpl")):
        ot = ies._run_open_trade_path(ies._rs(), tg_id="tg-atr-b", dpm_candles=None,
                                      bridge=_CandleIMEBridge())
    assert ot.called
    # 4.0 x 1.5 = 6.0 below the entry, the same stop the DPM-cache test pins.
    assert ot.call_args.kwargs["stop_loss"] == pytest.approx(2409.0)
