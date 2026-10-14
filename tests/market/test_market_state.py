"""docs/todo/017 item 4: one market-state read per signal, from H1 bars that had
CLOSED before the signal, so a stored state never knows the future.

Three states per axis by design (trend with / trend against / range;
low / normal / high volatility): more cells than that leave too few trades in
each to judge.
"""
from __future__ import annotations

import pytest

from backend.src.services.market import market_state as ms

H = 3600.0
T0 = 1_760_000_400.0 - (1_760_000_400.0 % H)   # an hour boundary


def _bars(closes, *, start=T0, rng=2.0):
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        out.append({"ts": start + i * H, "open": prev, "close": c,
                    "high": max(prev, c) + rng / 2, "low": min(prev, c) - rng / 2})
        prev = c
    return out


def _after(bars):
    """A moment just after the last bar closed."""
    return bars[-1]["ts"] + H + 1.0


class TestTheRegime:
    def test_a_clean_ramp_is_a_trend_up_and_a_buy_is_with_it(self):
        b = _bars([100 + i for i in range(30)])
        s = ms.snapshot(b, _after(b), "BUY")
        assert (s["h1_regime"], s["h1_dir"], s["h1_side"]) == ("trend", "up", "with")
        assert ms.snapshot(b, _after(b), "SELL")["h1_side"] == "against"

    def test_a_flat_chop_is_a_range(self):
        b = _bars([100 + (1 if i % 2 else -1) for i in range(30)])
        s = ms.snapshot(b, _after(b), "BUY")
        assert s["h1_regime"] == "range" and s["h1_side"] == "flat"

    def test_a_bar_still_open_at_the_signal_is_not_read(self):
        """Six falling bars, then one huge up bar that has NOT closed yet."""
        b = _bars([100 - i for i in range(30)] + [200])
        t = b[-1]["ts"] + 600.0      # ten minutes into the last bar
        s = ms.snapshot(b, t, "SELL")
        assert s["h1_dir"] == "down"

    def test_control_the_same_bar_once_closed_is_read(self):
        b = _bars([100 - i for i in range(30)] + [200])
        assert ms.snapshot(b, _after(b), "SELL")["h1_dir"] == "up"

    def test_too_few_bars_is_unmeasured_not_a_guess(self):
        b = _bars([100, 101, 102])
        s = ms.snapshot(b, _after(b), "BUY")
        assert s["h1_regime"] is None and s["h1_side"] is None


class TestVolatility:
    def test_a_quiet_hour_after_a_normal_week_reads_low(self):
        b = _bars([100.0] * 160, rng=4.0) + _bars([100.0] * 20, start=T0 + 160 * H, rng=1.0)
        s = ms.snapshot(b, _after(b), "BUY")
        assert s["vol_state"] == "low" and s["vol_ratio"] < ms.VOL_LOW

    def test_a_wild_hour_after_a_normal_week_reads_high(self):
        b = _bars([100.0] * 160, rng=2.0) + _bars([100.0] * 20, start=T0 + 160 * H, rng=8.0)
        assert ms.snapshot(b, _after(b), "BUY")["vol_state"] == "high"

    def test_steady_reads_normal(self):
        b = _bars([100.0] * 180, rng=2.0)
        s = ms.snapshot(b, _after(b), "BUY")
        assert s["vol_state"] == "normal" and s["vol_ratio"] == pytest.approx(1.0)

    def test_without_a_week_of_history_it_is_unmeasured(self):
        b = _bars([100.0] * 40)
        s = ms.snapshot(b, _after(b), "BUY")
        assert s["vol_ratio"] is None and s["vol_state"] is None


class TestTheClock:
    def test_session_hour_and_weekday_come_from_the_signal_time(self):
        t = 1_760_000_000.0   # 2025-10-09 08:53:20 UTC, a Thursday
        s = ms.snapshot([], t, "BUY")
        assert (s["hour"], s["weekday"], s["session"]) == (8, 3, "london")

    def test_the_version_is_stamped(self):
        assert ms.snapshot([], T0, "BUY")["v"] == ms.VERSION


class TestTheCell:
    @pytest.mark.parametrize("state,cell", [
        ({"h1_regime": "trend", "h1_side": "with"}, "trend_with"),
        ({"h1_regime": "trend", "h1_side": "against"}, "trend_against"),
        ({"h1_regime": "range", "h1_side": "flat"}, "range"),
        ({"h1_regime": None, "h1_side": None}, None),
    ])
    def test_regime_cell(self, state, cell):
        assert ms.regime_cell(state) == cell
