"""Profit factor and expectancy for any one signal source.

The Breakout engine got these figures first, out of its own database. The
Reversal Engine keeps no comparable table -- `realised_pnl_for_source` reads
`vantage_simulated_trades`, because an engine's own database prices every
signal it ever produced at the virtual lot whether or not the order was placed.

So this is the same pair of figures over the trades that REALLY went to MT5,
and it takes a source name rather than being welded to one engine: the same
query answers "does this Telegram channel have an edge", which is the question
the Analysis tab's scorecard raises and cannot answer.

The three-state rules are the Breakout ones, for the same reasons: a ratio
with a zero denominator does not exist yet rather than being zero, and a
source with no closed trades has no edge rather than a bad one.
"""
from __future__ import annotations

import uuid

import pytest

from backend.src.services.analytics import read_repo


def _trade(conn, source, net_pnl, status="closed", close_time=1000.0):
    """One closed trade for `source`, with the parent signal row it needs.

    `vantage_simulated_trades.signal_id` is a foreign key into
    `vantage_signals`, so a trade cannot be inserted on its own -- the same
    two-row shape `tests/reversal_engine/test_panel_realised_pnl.py` uses.
    """
    tid = uuid.uuid4().hex[:16]
    sid = f"sig-{tid}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", (sid, "BUY", 2399.0, 2401.0, 2390.0))
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, net_pnl, "
        " close_time, tg_source) VALUES (?,?,?,?,?,?,?,?,?,?,0,?,?,?)",
        (tid, sid, "BUY", 2399.0, 2401.0, 2400.0, 0.1, 0.1, 2390.0,
         status, net_pnl, close_time, source))


class TestTheRatio:

    def test_it_is_gross_profit_over_gross_loss(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", 300.0)
            _trade(conn, "Reversal Engine", -100.0)
            _trade(conn, "Reversal Engine", -50.0)

        assert read_repo.edge_for_source("Reversal Engine")["profit_factor"] == \
            pytest.approx(2.0)

    def test_a_source_that_has_never_lost_has_no_ratio(self, fresh_db):
        # Not infinity and not 0.0 -- 0.0 reads as the worst possible source.
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", 100.0)

        assert read_repo.edge_for_source("Reversal Engine")["profit_factor"] is None

    def test_a_source_that_has_never_won_scores_zero(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", -100.0)

        assert read_repo.edge_for_source("Reversal Engine")["profit_factor"] == \
            pytest.approx(0.0)


class TestExpectancy:

    def test_it_is_what_the_average_trade_returns(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", 100.0)
            _trade(conn, "Reversal Engine", 100.0)
            _trade(conn, "Reversal Engine", -50.0)
            _trade(conn, "Reversal Engine", -50.0)

        assert read_repo.edge_for_source("Reversal Engine")["expectancy"] == \
            pytest.approx(25.0)

    def test_it_agrees_with_the_per_trade_average_beside_it(self, fresh_db):
        # The two are computed by different SQL over the same rows, and a
        # disagreement between them on one screen is worse than either number
        # being absent.
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", 300.0)
            _trade(conn, "Reversal Engine", -100.0)

        edge = read_repo.edge_for_source("Reversal Engine")
        realised = read_repo.realised_pnl_for_source("Reversal Engine")

        assert edge["expectancy"] == pytest.approx(realised["per_trade"])

    def test_a_high_win_rate_can_still_be_negative(self, fresh_db):
        with fresh_db.db() as conn:
            for _ in range(3):
                _trade(conn, "Reversal Engine", 10.0)
            _trade(conn, "Reversal Engine", -100.0)

        edge = read_repo.edge_for_source("Reversal Engine")

        assert edge["win_rate"] == pytest.approx(75.0)
        assert edge["expectancy"] < 0


class TestWhatItCounts:

    def test_another_source_is_not_in_it(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", 100.0)
            _trade(conn, "GoldSignals", -500.0)

        edge = read_repo.edge_for_source("Reversal Engine")

        assert edge["closed"] == 1
        assert edge["gross_loss"] == pytest.approx(0.0)

    def test_an_open_trade_is_not_in_it(self, fresh_db):
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", 100.0)
            _trade(conn, "Reversal Engine", 999.0, status="open")

        assert read_repo.edge_for_source("Reversal Engine")["closed"] == 1

    def test_it_honours_the_reset_epoch(self, fresh_db):
        # A panel reset to report from a fresh start must not have its edge
        # figures quietly computed over everything before it.
        with fresh_db.db() as conn:
            _trade(conn, "Reversal Engine", -500.0, close_time=100.0)
            _trade(conn, "Reversal Engine", 50.0, close_time=2000.0)

        edge = read_repo.edge_for_source("Reversal Engine", since=1000.0)

        assert edge["closed"] == 1
        assert edge["expectancy"] == pytest.approx(50.0)

    def test_a_source_with_nothing_reports_nothing_rather_than_zeros(self, fresh_db):
        edge = read_repo.edge_for_source("Reversal Engine")

        assert edge["closed"] == 0
        assert edge["profit_factor"] is None
        assert edge["expectancy"] is None
