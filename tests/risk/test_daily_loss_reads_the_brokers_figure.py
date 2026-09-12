"""The daily-loss halt measures the broker's P&L, not the app's estimate.

`risk/repo.sum_realised_pnl_since` is what `governor.apply_daily_loss_halt_on_close`
sums to decide whether to stop trading for the day. Its name says
`realised_pnl`; its SQL sums **`net_pnl`**, and that difference is the whole
point of this file.

The two columns are not the same number once a trade has settled.
`profit_sync` replaces `net_pnl` and `mt5_profit` with the broker's own figure
when the deal history lands, and leaves `realised_pnl` holding whatever the app
computed at close time. On the live demo database on 2026-09-12 they disagreed
on **114 of 295 closed rows** -- usually by a few cents, once by $54 and with
the opposite sign (`net_pnl` -50.90 against `realised_pnl` +3.11).

So a well-meant tidy-up -- "the function is called realised, the column is
called realised" -- would silently switch a protective limit from the broker's
truth to the app's estimate. Nothing else tests which column it reads: every
existing test of the halt monkeypatches this function away.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.risk import repo as risk_repo


def _closed_trade(db, trade_id: str, *, net: float, realised: float, close_time: float):
    """Insert through the real table, NOT NULL constraints and all.

    A permissive fake is how bugs/019 stayed hidden through two passing tests:
    the fake accepted a row the real schema refuses.
    """
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals "
            "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
            "VALUES (?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", 3999.0, 4001.0, 3950.0, close_time - 120.0),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades "
            "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
            " lot_size, remaining_lots, stop_loss, status, open_time, close_time, "
            " net_pnl, realised_pnl, gross_pnl) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", "BUY", 3999.0, 4001.0, 4000.0,
             0.1, 0.0, 3950.0, "closed", close_time - 60.0, close_time,
             net, realised, realised),
        )


class TestWhichColumnDecidesTheHalt:
    def test_the_sum_follows_the_brokers_corrected_figure(self, fresh_db):
        """`net_pnl` is what profit_sync corrects. A day that the app thought
        was flat and the broker says lost $200 must halt on the $200."""
        now = time.time()
        _closed_trade(fresh_db, "t1", net=-200.0, realised=0.0, close_time=now)

        assert risk_repo.sum_realised_pnl_since(now - 3600) == -200.0

    def test_it_does_not_follow_the_apps_own_estimate(self, fresh_db):
        """The sign-flip case, taken from the live ledger: the estimate says
        this trade won, the broker says it lost."""
        now = time.time()
        _closed_trade(fresh_db, "t1", net=-50.90, realised=3.11, close_time=now)

        total = risk_repo.sum_realised_pnl_since(now - 3600)

        assert total == pytest.approx(-50.90)
        assert total != pytest.approx(3.11)


class TestTheWindow:
    def test_a_trade_closed_before_the_day_started_is_not_counted(self, fresh_db):
        """The halt is a per-day limit. Counting yesterday's losses would halt
        a day that has not lost anything yet."""
        now = time.time()
        _closed_trade(fresh_db, "yesterday", net=-500.0, realised=-500.0,
                      close_time=now - 86400)
        _closed_trade(fresh_db, "today", net=-10.0, realised=-10.0, close_time=now)

        assert risk_repo.sum_realised_pnl_since(now - 3600) == -10.0

    def test_a_trade_closed_exactly_at_the_boundary_counts(self, fresh_db):
        """`>=`, not `>`. The first trade of the day closing on the stroke of
        the boundary belongs to the day it opened into."""
        now = time.time()
        _closed_trade(fresh_db, "boundary", net=-10.0, realised=-10.0, close_time=now)

        assert risk_repo.sum_realised_pnl_since(now) == -10.0

    def test_a_day_with_nothing_closed_sums_to_zero_not_none(self, fresh_db):
        """It feeds a comparison. None there is a TypeError inside the halt
        check, on the close path, which fails the protective check open."""
        assert risk_repo.sum_realised_pnl_since(time.time()) == 0.0
