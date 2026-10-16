"""Forecast-volatility stops (docs/todo/019 B).

The HAR fit, its conversion into M5-ATR units, the clamp, and the one place
it is read: `template_levels.template_atr`. Nothing here reaches a broker:
the bridge is a fake that records which timeframes were asked for and
returns canned candles. Pinned: with the setting off `template_atr` asks for
nothing but the 20 M5 bars it always did and returns the same figure; on, a
failed or thin H1 read falls back to that same M5 ATR.
"""
import asyncio
import random

import pytest

from backend.src.db import database as db
from backend.src.services.dpm.engine import compute_atr
from backend.src.services.market import vol_forecast as vf
from backend.src.services.trading import template_levels as tl


def _bars(trs, price=2000.0, t0=0, step=3600):
    """Candles whose true range is exactly `trs[i]` (open = close = price)."""
    return [{"time": t0 + i * step, "open": price, "close": price,
             "high": price + tr / 2, "low": price - tr / 2} for i, tr in enumerate(trs)]


def _har_series(n, c=0.5, a=0.3, b=0.3, g=0.2, seed=7):
    rng = random.Random(seed)
    tr = [4.0] * vf.WEEK_H
    while len(tr) < n:
        d, w = tr[-1], sum(tr[-vf.DAY_H:]) / vf.DAY_H
        m = sum(tr[-vf.WEEK_H:]) / vf.WEEK_H
        tr.append(max(0.1, c + a * d + b * w + g * m + rng.gauss(0, 0.3)))
    return tr


class TestHar:
    def test_recovers_known_coefficients(self):
        tr = _har_series(4000)
        coef = vf.har_fit(tr)
        assert coef == pytest.approx([0.5, 0.3, 0.3, 0.2], abs=0.12)

    def test_the_forecast_uses_the_last_hour_day_and_week(self):
        tr = _har_series(800)
        coef = vf.har_fit(tr)
        d, w = tr[-1], sum(tr[-vf.DAY_H:]) / vf.DAY_H
        m = sum(tr[-vf.WEEK_H:]) / vf.WEEK_H
        want = coef[0] + coef[1] * d + coef[2] * w + coef[3] * m
        assert vf.har_forecast(_bars(tr)) == pytest.approx(want)

    def test_too_few_rows_is_no_forecast(self):
        assert vf.har_forecast(_bars([4.0] * (vf.WEEK_H + vf.MIN_ROWS - 1))) is None

    def test_a_flat_series_is_no_forecast(self):
        assert vf.har_forecast(_bars([4.0] * 600)) is None     # singular fit


class TestUnits:
    def test_scaled_by_m5_over_h1_true_range(self):
        h1 = _bars([4.0] * 48)
        m5 = _bars([1.0] * (48 * 12), step=300)
        assert vf.to_m5_units(8.0, h1, m5) == pytest.approx(2.0)

    @pytest.mark.parametrize("raw,want", [(100.0, 3.0), (0.1, 0.75), (2.0, 2.0)])
    def test_clamped_to_half_and_double_the_m5_atr(self, raw, want, monkeypatch):
        monkeypatch.setattr(vf, "har_forecast", lambda h1: raw)
        monkeypatch.setattr(vf, "to_m5_units", lambda f, h1, m5: f)
        assert vf.forecast_atr([], [], m5_atr=1.5) == pytest.approx(want)

    def test_no_forecast_is_none(self, monkeypatch):
        monkeypatch.setattr(vf, "har_forecast", lambda h1: None)
        assert vf.forecast_atr([], [], m5_atr=1.5) is None


class _Bridge:
    def __init__(self, h1=None, fail_h1=False):
        self.asked = []
        self.h1, self.fail_h1 = h1, fail_h1
        self.m5 = _bars([1.0 + 0.1 * (i % 5) for i in range(700)], step=300)

    async def get_candles(self, timeframe="M5", count=200):
        self.asked.append((timeframe, count))
        if timeframe == "H1":
            if self.fail_h1:
                raise RuntimeError("bridge down")
            return (self.h1 or [])[-count:]
        return self.m5[-count:]


TPL = {"use_dynamic_atr": True, "atr_period": 14}


@pytest.fixture
def setting(monkeypatch):
    state = {"vol_forecast_stops_enabled": 0, "round_number_exits_enabled": 0}
    monkeypatch.setattr(db, "get_risk_settings", lambda: dict(state))
    tl.reset_atr_cache()
    yield state
    tl.reset_atr_cache()


def _atr(bridge):
    return asyncio.run(tl.template_atr(TPL, bridge))


class TestTemplateAtr:
    def test_off_asks_only_for_20_m5_bars_and_returns_the_m5_atr(self, setting):
        b = _Bridge(h1=_bars(_har_series(700)))
        assert _atr(b) == compute_atr(b.m5[-20:], period=14)
        assert b.asked == [("M5", 20)]

    def test_on_returns_the_forecast(self, setting):
        setting["vol_forecast_stops_enabled"] = 1
        h1 = _bars(_har_series(700))
        b = _Bridge(h1=h1)
        m5_atr = compute_atr(b.m5[-20:], period=14)
        want = vf.forecast_atr(h1[:-1][-vf.FIT_BARS:], b.m5[-(vf.SCALE_H * 12 + 1):-1], m5_atr)
        assert want is not None and want != m5_atr
        assert _atr(b) == pytest.approx(want)
        assert ("H1", vf.FIT_BARS + 1) in b.asked

    @pytest.mark.parametrize("bridge", [lambda: _Bridge(fail_h1=True), lambda: _Bridge(h1=[])])
    def test_on_falls_back_to_the_m5_atr(self, setting, bridge):
        setting["vol_forecast_stops_enabled"] = 1
        b = bridge()
        assert _atr(b) == compute_atr(b.m5[-20:], period=14)

    def test_on_and_off_do_not_share_a_cached_figure(self, setting):
        b = _Bridge(h1=_bars(_har_series(700)))
        off = _atr(b)
        setting["vol_forecast_stops_enabled"] = 1
        assert _atr(b) != off
