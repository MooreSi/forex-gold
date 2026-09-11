"""Teach the template walk ATR sizing, so the recommended template can be
backtested at all.

docs/todo/reversal-engine/200 section 2: the "Reversal ATR v1" preset sizes
its stop and its whole ladder from ATR, and `template_support` refused
`use_dynamic_atr` outright because "the historical walk cannot reproduce a
live ATR". That refusal was right when nothing supplied one. The bar walk
has the candles: `engine._atr14` already computes exactly this number a few
lines from the call site.

**The tick walk still refuses**, and for the same reason `trail_mode=candle`
does: a tick series has no candle series to derive an ATR from, and
inventing one would diverge from the EA silently.
"""
from __future__ import annotations

import pytest

from backend.src.services.backtest import template_simulator as ts
from backend.src.services.backtest.template_support import can_simulate


def bars(prices, high_pad=0.5, low_pad=0.5):
    return [{"ts": 1000.0 + i * 60, "open": p, "high": p + high_pad,
             "low": p - low_pad, "close": p} for i, p in enumerate(prices)]


def template(**kw):
    base = {"mode": "single", "pendings": 0, "sl_pips": 50.0,
            "tp1_pips": 100.0, "tp1_pct": 100.0, "trail_mode": "off",
            "lot_anchor": 0.10, "use_dynamic_atr": True,
            "atr_sl_mult": 1.5, "atr_tp1_mult": 1.5}
    base.update(kw)
    return base


class TestTheRefusal:
    def test_it_still_refuses_when_no_atr_is_available(self):
        ok, reasons = can_simulate(template())
        assert ok is False
        assert any("atr" in r.lower() for r in reasons)

    def test_it_accepts_when_the_caller_can_supply_one(self):
        ok, _reasons = can_simulate(template(), atr_available=True)
        assert ok is True

    def test_the_tick_walk_refuses_regardless(self):
        """A tick series has no candles to derive an ATR from. Same
        argument as trail_mode=candle, which this walk already refuses."""
        assert "atr" in ts.unsupported_reason(template(), tick_mode=True).lower()

    def test_a_template_without_dynamic_atr_is_unaffected(self):
        assert can_simulate(template(use_dynamic_atr=False))[0] is True


class TestTheStop:
    def test_the_stop_is_the_atr_multiple_not_sl_pips(self):
        """sl_pips 50 is 5.0 in price. ATR 2.0 x 1.5 is 3.0. A walk that
        used sl_pips here would report a different trade from the one the
        live path would take."""
        res = ts.simulate(template(), bars([3300.0, 3296.9]), entry=3300.0,
                          is_buy=True, atr=2.0)
        assert res.close_price == pytest.approx(3297.0)
        assert res.outcome == "sl"

    def test_an_atr_template_with_no_atr_is_refused_not_silently_resized(self):
        """Falling back to sl_pips here would be the worst outcome: a
        backtest reporting a 50-pip stop for a template that trades a
        1.5x ATR one. The refusal is the honest answer."""
        with pytest.raises(ts.UnsupportedTemplate):
            ts.simulate(template(), bars([3300.0, 3294.9]), entry=3300.0,
                        is_buy=True, atr=0.0)

    def test_a_template_without_dynamic_atr_uses_sl_pips_as_before(self):
        res = ts.simulate(template(use_dynamic_atr=False),
                          bars([3300.0, 3294.9]), entry=3300.0, is_buy=True)
        assert res.close_price == pytest.approx(3295.0)


class TestTheLadder:
    def test_tp1_is_the_atr_multiple(self):
        res = ts.simulate(template(), bars([3300.0, 3303.1]), entry=3300.0,
                          is_buy=True, atr=2.0)
        assert res.close_price == pytest.approx(3303.0)
        assert res.outcome == "tp"

    def test_the_whole_ladder_scales_when_atr_ladder_scale_is_on(self):
        """TP2 was twice TP1 in pips and stays twice TP1 after scaling --
        the shape survives and only its size changes. Without this the
        stop and TP1 move with volatility and TP2 does not."""
        t = template(atr_ladder_scale=True, tp1_pct=50.0,
                     tp2_pips=200.0, tp2_pct=50.0)
        res = ts.simulate(t, bars([3300.0, 3303.1, 3306.1]), entry=3300.0,
                          is_buy=True, atr=2.0)
        assert res.close_price == pytest.approx(3306.0)

    def test_with_ladder_scaling_off_only_tp1_moves(self):
        t = template(atr_ladder_scale=False, tp1_pct=50.0,
                     tp2_pips=200.0, tp2_pct=50.0)
        res = ts.simulate(t, bars([3300.0, 3303.1, 3320.1]), entry=3300.0,
                          is_buy=True, atr=2.0)
        # TP2 stays at its fixed 200 pips = 20.0 in price.
        assert res.close_price == pytest.approx(3320.0)
