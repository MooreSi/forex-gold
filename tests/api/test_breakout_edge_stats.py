"""Profit factor and expectancy for the Breakout engine.

The NiceGUI app had an Edge tab -- `ui/pages/edge_dashboard.py` -- whose whole
job was the numbers that decide whether an engine has an edge at all: profit
factor, expectancy, win rate, and average win against average loss. It is the
one page the React port has no counterpart for.

A win rate on its own decides nothing. 38% with an average win three times the
average loss is a profitable engine; 60% with the ratio inverted is not. These
two figures are what separate those cases, and neither exists anywhere in the
app today.

Computed in SQL over the engine's own closed rows rather than in the browser:
gross profit and gross loss are two sums the database can do in one pass, and
a browser deriving them from a paginated list would be deriving them from a
page.

Its own module because `breakout_signal_repo.py` sits at the 800-line ceiling
and this asks a different question: that file records what the engine DID,
this asks whether any of it amounts to an edge.
"""
from __future__ import annotations

import sqlite3

import pytest

from backend.src.services.breakout_signal import breakout_edge_repo as repo


@pytest.fixture
def engine_db(tmp_path, monkeypatch):
    """A bo_signals table with nothing in it but what each test puts there."""
    path = tmp_path / "breakout_signal.db"
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE bo_signals ("
        " id INTEGER PRIMARY KEY AUTOINCREMENT,"
        " status TEXT, outcome TEXT, pnl_dollars REAL, pnl_pts REAL)"
    )
    conn.commit()
    conn.close()

    class _Db:
        def get(self, sql, *params):
            # A real sqlite3.Row, NOT a dict. The first version of this
            # fixture returned a dict, so the implementation could call
            # `row.get(...)` -- which a Row does not have -- and pass here
            # while every live read failed into the router's guard. A stub
            # that is easier to satisfy than the real thing proves nothing.
            c = sqlite3.connect(str(path))
            c.row_factory = sqlite3.Row
            try:
                return c.execute(sql, params).fetchone()
            finally:
                c.close()

        def all(self, sql, *params):
            c = sqlite3.connect(str(path))
            c.row_factory = sqlite3.Row
            try:
                return [dict(r) for r in c.execute(sql, params).fetchall()]
            finally:
                c.close()

    monkeypatch.setattr(repo, "get_db", lambda: _Db())
    return path


def _trade(path, outcome, dollars, status="closed"):
    conn = sqlite3.connect(str(path))
    conn.execute(
        "INSERT INTO bo_signals (status, outcome, pnl_dollars, pnl_pts) VALUES (?,?,?,?)",
        (status, outcome, dollars, dollars / 10.0))
    conn.commit()
    conn.close()


class TestProfitFactor:

    def test_it_is_gross_profit_over_gross_loss(self, engine_db):
        _trade(engine_db, "win", 300.0)
        _trade(engine_db, "loss", -100.0)
        _trade(engine_db, "loss", -50.0)

        assert repo.get_edge_stats()["profit_factor"] == pytest.approx(2.0)

    def test_an_engine_that_has_never_lost_has_no_ratio(self, engine_db):
        # Not "infinity" and not 0.0. A ratio with a zero denominator is not a
        # large number, it is a number that does not exist yet -- and 0.0
        # reads as the worst possible engine.
        _trade(engine_db, "win", 100.0)

        assert repo.get_edge_stats()["profit_factor"] is None

    def test_an_engine_that_has_never_won_scores_zero(self, engine_db):
        # This one IS zero: it has a denominator and no numerator.
        _trade(engine_db, "loss", -100.0)

        assert repo.get_edge_stats()["profit_factor"] == pytest.approx(0.0)


class TestExpectancy:

    def test_it_is_what_the_average_trade_returns(self, engine_db):
        # Two wins of 100 and two losses of 50: (0.5 x 100) - (0.5 x 50) = 25.
        _trade(engine_db, "win", 100.0)
        _trade(engine_db, "win", 100.0)
        _trade(engine_db, "loss", -50.0)
        _trade(engine_db, "loss", -50.0)

        assert repo.get_edge_stats()["expectancy"] == pytest.approx(25.0)

    def test_a_high_win_rate_can_still_be_negative(self, engine_db):
        # The whole reason the figure exists. 75% wins, and losing money.
        for _ in range(3):
            _trade(engine_db, "win", 10.0)
        _trade(engine_db, "loss", -100.0)

        stats = repo.get_edge_stats()

        assert stats["win_rate"] == pytest.approx(75.0)
        assert stats["expectancy"] < 0

    def test_it_reports_the_two_averages_it_is_built_from(self, engine_db):
        # So the figure can be argued with rather than just believed.
        _trade(engine_db, "win", 300.0)
        _trade(engine_db, "loss", -100.0)

        stats = repo.get_edge_stats()

        assert stats["avg_win"] == pytest.approx(300.0)
        assert stats["avg_loss"] == pytest.approx(100.0)


class TestWhatItCounts:

    def test_an_open_trade_is_not_in_it(self, engine_db):
        _trade(engine_db, "win", 100.0)
        _trade(engine_db, "open", 999.0, status="triggered")

        assert repo.get_edge_stats()["closed"] == 1

    def test_a_breakeven_trade_counts_as_a_trade(self, engine_db):
        # It is neither a win nor a loss, and leaving it out inflates both
        # the win rate and the expectancy.
        _trade(engine_db, "win", 100.0)
        _trade(engine_db, "be", 0.0)

        stats = repo.get_edge_stats()

        assert stats["closed"] == 2
        assert stats["win_rate"] == pytest.approx(50.0)

    def test_an_empty_engine_reports_nothing_rather_than_zeros(self, engine_db):
        # A fresh install has no edge, not a bad one.
        stats = repo.get_edge_stats()

        assert stats["closed"] == 0
        assert stats["profit_factor"] is None
        assert stats["expectancy"] is None
