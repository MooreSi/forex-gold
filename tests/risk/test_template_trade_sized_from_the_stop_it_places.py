"""A template trade on the queued path is sized from the stop it actually places.

Owner, 2026-09-29: "% of available capital" gave 0.05, 0.04, 0.03, 0.02 and
0.07 lots on one template ("30 TP1 SL50 and Trail", sl_pips 50, single leg,
Risk 2%, EA template override on).

`resolve_open_trade_params` (the queued path: the engines, and a Telegram
signal that arrives out of its zone) sized the lot from the SIGNAL's stop, then
replaced that stop with the template's `sl_pips` before the order. A channel
posting a 2-point stop got 2.5x the lot, and the trade went on with the
template's 5-point stop: about 5% of the balance at risk against a configured
2%. A 7.5-point signal stop got under half the risk.

Instant entry and the Reversal Engine's limit orders already sized from the
template's stop (instant_entry.py, reversal_engine_live_execute.py). This pins
the queued path to the same rule. A template with no stop of its own
(sl_pips = 0) keeps the signal's stop for both, as before.

The bridge is a fake that returns a canned tick and balance. Nothing here
reaches a broker, live or demo.
"""
from __future__ import annotations

import asyncio

from backend.src.db import database as db
from backend.src.services.broker import ea_templates as et
from backend.src.services.signals import resolution as sr
from tests.core.test_signal_resolution_surface import (
    _FakeBridge as _ResolveBridge, _insert_signal,
)

# Risk 2% of 10,000 = $200. Ceilings well above every result, so neither the
# Maximum lot size nor the Max Risk per trade % decides any number below.
_RISK_2PCT = {"global_sizing_override": 1, "strategy_lot_size": 0,
              "max_lot_size": 5.0, "risk_per_trade_pct": 2.0,
              "max_risk_per_trade_pct": 10.0}


def _resolve(template: dict, signal_stop: float, sig_id: str = "sig-1") -> dict:
    et.save_ea_template("SL50", dict({"mode": "single", "risk_pct": 0}, **template))
    db.set_channel_strategy_override("QChan", et.override_for_template("SL50"))
    db.update_risk_settings(_RISK_2PCT)
    _insert_signal(sig_id=sig_id, source_name="QChan", stop_loss=signal_stop)  # zone 2399-2401
    return asyncio.run(sr.resolve_open_trade_params(
        _ResolveBridge(account={"balance": 10000.0}), sig_id))


def _risk_usd(r: dict) -> float:
    """What the trade loses at the stop it is sent with, in dollars (100 oz a
    lot), filled at the tick the stop is measured from."""
    fill = r["tick"].ask if r["sig"]["direction"].upper() == "BUY" else r["tick"].bid
    return abs(fill - r["stop_loss_to_use"]) * r["lot_size"] * 100


class TestTheTemplatesStopSizesTheLot:
    def test_A_TIGHT_SIGNAL_STOP_DOES_NOT_INFLATE_THE_LOT(self, fresh_db):
        """The 0.07: a 2-point signal stop, a 5-point template stop.
        $200 over 5 points = 0.40 lots, not $200 over 2 points = 1.00."""
        r = _resolve({"sl_pips": 50}, signal_stop=2398.0)
        assert r["lot_size"] == 0.40

    def test_a_wide_signal_stop_does_not_shrink_it(self, fresh_db):
        """The 0.02: a 10-point signal stop. Still 0.40, not 0.20."""
        r = _resolve({"sl_pips": 50}, signal_stop=2390.0)
        assert r["lot_size"] == 0.40

    def test_the_money_at_risk_is_the_configured_two_percent(self, fresh_db):
        for signal_stop in (2398.0, 2395.0, 2390.0):
            r = _resolve({"sl_pips": 50}, signal_stop=signal_stop, sig_id=f"sig-{signal_stop}")
            assert abs(_risk_usd(r) - 200.0) < 5.0, (signal_stop, r["lot_size"], r["stop_loss_to_use"])

    def test_a_template_without_a_stop_keeps_sizing_from_the_signal(self, fresh_db):
        """Negative control: sl_pips 0 means the signal's stop is placed, so
        the signal's stop sizes it. 10 points: 0.20."""
        r = _resolve({"sl_pips": 0}, signal_stop=2390.0)
        assert r["stop_loss_to_use"] == 2390.0
        assert r["lot_size"] == 0.20

    def test_fixed_lots_are_untouched_by_either_stop(self, fresh_db):
        db.update_risk_settings({"strategy_lot_size": 0.03})
        et.save_ea_template("SL50", {"mode": "single", "risk_pct": 0, "sl_pips": 50})
        db.set_channel_strategy_override("QChan", et.override_for_template("SL50"))
        db.update_risk_settings({"global_sizing_override": 1, "max_lot_size": 5.0})
        _insert_signal(source_name="QChan", stop_loss=2398.0)
        r = asyncio.run(sr.resolve_open_trade_params(
            _ResolveBridge(account={"balance": 10000.0}), "sig-1"))
        assert r["lot_size"] == 0.03
