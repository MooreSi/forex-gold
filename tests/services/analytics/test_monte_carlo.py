"""Monte Carlo on the account's own closed trades (docs/todo/024).

The research lab bootstraps backtests; nothing answered "with these real
trades at this size, how likely is a 20% drawdown?". This resamples the
window's net P&L into many possible next-N-trade paths from the current
balance and reports what they did.

Pure arithmetic on canned numbers: no broker, no database.
"""
from __future__ import annotations

import pytest

from backend.src.services.analytics import monte_carlo as mc


def _mixed(n=60):
    # 60% winners of +30, 40% losers of -25.
    return [30.0 if i % 5 < 3 else -25.0 for i in range(n)]


class TestMaxDrawdown:
    def test_a_known_path(self):
        # 1000 -> 1100 -> 880 -> 990 -> 1210: worst is 1100 -> 880, 20%.
        assert mc.max_drawdown_pct(1000.0, [100.0, -220.0, 110.0, 220.0]) == pytest.approx(20.0)

    def test_the_starting_balance_is_a_peak(self):
        assert mc.max_drawdown_pct(1000.0, [-100.0, 50.0]) == pytest.approx(10.0)

    def test_a_path_that_only_rises_has_none(self):
        assert mc.max_drawdown_pct(1000.0, [10.0, 20.0]) == 0.0

    def test_losing_the_account_is_a_hundred_percent_not_more(self):
        assert mc.max_drawdown_pct(100.0, [-80.0, -80.0]) == 100.0


class TestSimulate:
    def test_too_few_trades_gives_a_note_and_no_numbers(self):
        got = mc.simulate([10.0] * (mc.MIN_TRADES - 1), 1000.0, 50)

        assert got["note"]
        assert "max_dd_pct" not in got

    def test_the_same_inputs_give_the_same_answer(self):
        a = mc.simulate(_mixed(), 5000.0, 100)
        b = mc.simulate(_mixed(), 5000.0, 100)

        assert a == b

    def test_all_winners_never_draw_down(self):
        got = mc.simulate([20.0] * 40, 1000.0, 100)

        assert got["prob_dd"] == {"10": 0.0, "20": 0.0, "30": 0.0, "50": 0.0}
        assert got["prob_loss"] == 0.0
        # 100 trades of +20 from 1000.
        assert got["final"]["p50"] == pytest.approx(3000.0)

    def test_a_drawdown_of_exactly_the_threshold_counts(self):
        # Every trade is -$1, so every path is the same: 100 -> 90, a 10% fall
        # from the STARTING balance, which is the first peak.
        got = mc.simulate([-1.0] * 40, 100.0, 10)

        assert got["prob_dd"]["10"] == 1.0
        assert got["prob_dd"]["20"] == 0.0
        assert got["max_dd_pct"]["p50"] == pytest.approx(10.0)

    def test_a_single_large_loser_makes_drawdown_possible(self):
        pnls = [20.0] * 39 + [-400.0]
        got = mc.simulate(pnls, 1000.0, 100)

        assert got["prob_dd"]["20"] > 0.0

    def test_probabilities_fall_as_the_threshold_rises(self):
        got = mc.simulate(_mixed(), 1000.0, 200)
        p = got["prob_dd"]

        assert 1.0 >= p["10"] >= p["20"] >= p["30"] >= p["50"] >= 0.0
        assert p["10"] > p["50"]

    def test_percentiles_are_ordered(self):
        got = mc.simulate(_mixed(), 5000.0, 100)
        f, d = got["final"], got["max_dd_pct"]

        assert f["p5"] <= f["p25"] <= f["p50"] <= f["p75"] <= f["p95"]
        assert d["p50"] <= d["p95"] <= d["p99"]

    def test_the_median_tracks_the_mean_trade(self):
        # Mean trade +8 ($30*0.6 - $25*0.4), 100 trades -> about +800.
        got = mc.simulate(_mixed(), 5000.0, 100)

        assert got["final"]["p50"] == pytest.approx(5800.0, abs=150.0)

    def test_the_band_starts_at_the_balance_and_has_bounded_length(self):
        got = mc.simulate(_mixed(), 5000.0, 500)
        band = got["band"]

        assert band[0] == {"n": 0, "p5": 5000.0, "p50": 5000.0, "p95": 5000.0}
        assert len(band) <= mc.BAND_POINTS + 1
        assert band[-1]["n"] == 500

    def test_it_reports_what_it_was_given(self):
        got = mc.simulate(_mixed(), 5000.0, 100)

        assert got["trades"] == 60
        assert got["horizon"] == 100
        assert got["balance"] == 5000.0
        assert got["paths"] == mc.PATHS

    def test_horizon_is_bounded(self):
        assert mc.simulate(_mixed(), 5000.0, 10_000)["horizon"] == mc.MAX_HORIZON
        assert mc.simulate(_mixed(), 5000.0, 0)["horizon"] == 1

    def test_no_balance_gives_a_note(self):
        assert mc.simulate(_mixed(), 0.0, 100)["note"]


class _Engine:
    def __init__(self, balance):
        self.balance = balance

    async def get_mt5_account(self):
        return None if self.balance is None else {"balance": self.balance}


@pytest.mark.asyncio
class TestForWindow:
    async def test_it_uses_the_tables_pnl_and_the_broker_balance(self, monkeypatch):
        async def _table(engine, days, today=False):
            return {"rows": [{"pnl": p} for p in _mixed()], "error": None}
        monkeypatch.setattr(mc._trade_table, "closed_trades", _table)

        got = await mc.for_window(_Engine(5000.0), 30, None)

        assert got["trades"] == 60
        assert got["balance"] == 5000.0
        # No horizon asked for: as many trades as the window held.
        assert got["horizon"] == 60

    async def test_a_broker_error_is_said_not_simulated(self, monkeypatch):
        async def _table(engine, days, today=False):
            return {"rows": [], "error": "MT5 deal history is unavailable."}
        monkeypatch.setattr(mc._trade_table, "closed_trades", _table)

        got = await mc.for_window(_Engine(5000.0), 30, 100)

        assert got["note"] == "MT5 deal history is unavailable."

    async def test_no_account_is_said(self, monkeypatch):
        async def _table(engine, days, today=False):
            return {"rows": [{"pnl": p} for p in _mixed()], "error": None}
        monkeypatch.setattr(mc._trade_table, "closed_trades", _table)

        got = await mc.for_window(_Engine(None), 30, 100)

        assert got["note"]
