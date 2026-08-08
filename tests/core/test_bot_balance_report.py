"""core_bot_balance_report.py -- the Telegram panel's Balance screen.

Bucketing money by date is where a report quietly goes wrong: an off-by-one
on the week boundary, a trade counted in two periods, or "today" resolved in
UTC while the user reads it in their own timezone. Those errors do not look
like errors on screen -- they look like a bad week. So the tests below fix
`now` and assert the arithmetic rather than the prose.

Read-only: nothing here places, closes or modifies an order.
"""
import asyncio
import os
import tempfile
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from forex_trader.core import core_bot_balance_report as report
from forex_trader.core import database as db


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


# Wednesday 12 Aug 2026, mid-afternoon. Fixed so week/month boundaries are
# arithmetic, not whatever today happens to be.
NOW = datetime(2026, 8, 12, 15, 30)


def _closed(trade_id: str, when: datetime, profit: float, use_net_pnl: bool = False):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", 2400.0, 2400.0, 2390.0, "active", when.timestamp()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, close_time, close_price, net_pnl, mt5_profit) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", "BUY", 2400.0, 2400.0, 2400.0, 0.1, 0.1, 2390.0,
             "closed", when.timestamp() - 600, when.timestamp(), 2410.0,
             profit if use_net_pnl else 0.0, None if use_net_pnl else profit),
        )


class _FakeBridge:
    def __init__(self, account=None, tick=None):
        self._account = account or {}
        self._tick = tick

    async def get_account(self):
        return self._account

    async def get_tick(self):
        return self._tick


# ── Bucketing ─────────────────────────────────────────────────────────────────

def test_week_starts_on_monday_and_covers_seven_days(fresh_db):
    totals = report.period_totals(NOW)
    assert totals["week_start"] == datetime(2026, 8, 10)      # Monday
    assert len(totals["days"]) == 7
    assert max(totals["days"]) == datetime(2026, 8, 16)       # Sunday


def test_a_trade_lands_in_its_own_day_week_and_month(fresh_db):
    _closed("t-1", NOW.replace(hour=9), 30.0)
    totals = report.period_totals(NOW)
    assert totals["today"].pnl == pytest.approx(30.0)
    assert totals["week"].pnl == pytest.approx(30.0)
    assert totals["month"].pnl == pytest.approx(30.0)
    assert totals["days"][datetime(2026, 8, 12)].pnl == pytest.approx(30.0)


def test_last_weeks_trade_counts_toward_the_month_but_not_the_week(fresh_db):
    """The bug this guards is a month total quietly built from the week's
    rows -- it agrees with the week every Monday and drifts all month."""
    _closed("t-1", datetime(2026, 8, 5, 11, 0), 100.0)        # previous week
    _closed("t-2", NOW.replace(hour=9), 25.0)
    totals = report.period_totals(NOW)
    assert totals["week"].pnl == pytest.approx(25.0)
    assert totals["month"].pnl == pytest.approx(125.0)


def test_a_trade_from_last_month_is_excluded_entirely(fresh_db):
    _closed("t-1", datetime(2026, 7, 30, 11, 0), 500.0)
    totals = report.period_totals(NOW)
    assert totals["month"].count == 0
    assert totals["week"].count == 0


def test_a_week_spanning_a_month_boundary_keeps_both_periods_honest(fresh_db):
    """Tue 1 Sep 2026: the week began Mon 31 Aug. The Monday belongs to the
    week but not the month -- a single cutoff for both would either lose it
    from the week or smuggle it into the month."""
    now = datetime(2026, 9, 1, 10, 0)
    _closed("t-aug", datetime(2026, 8, 31, 12, 0), 40.0)
    _closed("t-sep", datetime(2026, 9, 1, 9, 0), 10.0)
    totals = report.period_totals(now)
    assert totals["week"].pnl == pytest.approx(50.0)
    assert totals["month"].pnl == pytest.approx(10.0)


def test_open_trades_are_not_counted_as_realised(fresh_db):
    _closed("t-1", NOW.replace(hour=9), 30.0)
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET status='open', close_time=NULL "
                     "WHERE trade_id='t-1'")
    assert report.period_totals(NOW)["month"].count == 0


def test_broker_profit_wins_over_net_pnl(fresh_db):
    """mt5_profit includes swap and commission; net_pnl is our own estimate.
    Reporting the estimate when the broker has spoken makes the week's total
    disagree with the account balance."""
    _closed("t-1", NOW.replace(hour=9), 30.0)
    with db.db() as conn:
        conn.execute("UPDATE vantage_simulated_trades SET net_pnl=99.0 WHERE trade_id='t-1'")
    assert report.period_totals(NOW)["today"].pnl == pytest.approx(30.0)


def test_net_pnl_is_used_when_the_broker_reported_nothing(fresh_db):
    _closed("t-1", NOW.replace(hour=9), 42.0, use_net_pnl=True)
    assert report.period_totals(NOW)["today"].pnl == pytest.approx(42.0)


def test_win_rate_ignores_scratch_trades(fresh_db):
    """A trade closed exactly flat is neither a win nor a loss; counting it
    as a loss understates the rate."""
    _closed("t-1", NOW.replace(hour=9), 10.0)
    _closed("t-2", NOW.replace(hour=10), 0.0)
    _closed("t-3", NOW.replace(hour=11), -5.0)
    today = report.period_totals(NOW)["today"]
    assert (today.count, today.wins, today.losses) == (3, 1, 1)


# ── Rendering ─────────────────────────────────────────────────────────────────

def _render(bridge=None, now=NOW):
    return asyncio.run(report.build_balance_report(bridge or _FakeBridge(), now))


def test_report_shows_account_today_week_and_month(fresh_db):
    _closed("t-1", NOW.replace(hour=9), 30.0)
    text = _render(_FakeBridge(account={"balance": 1234.56, "equity": 1240.0,
                                        "margin_free": 1100.0}))
    assert "Balance:     $1,234.56" in text
    assert "*Today — Wed 12 Aug*" in text
    assert "*This Week — from Mon 10 Aug*" in text
    assert "*This Month — August 2026*" in text
    assert "+$30.00" in text


def test_every_day_of_the_week_gets_a_row(fresh_db):
    text = _render()
    for label in ("Mon 10 Aug", "Tue 11 Aug", "Wed 12 Aug", "Thu 13 Aug",
                  "Sun 16 Aug"):
        assert f"{label}:" in text


def test_a_day_still_to_come_is_not_reported_as_a_flat_day(fresh_db):
    text = _render()
    rows = {line.split(":")[0]: line for line in text.splitlines() if ":" in line}
    assert "to come" in rows["Thu 13 Aug"]
    assert rows["Tue 11 Aug"].endswith("—")          # past, nothing traded


def test_today_is_marked_in_the_week_rows(fresh_db):
    assert "Wed 12 Aug:" in _render()
    assert "← today" in _render()


def test_losses_are_signed(fresh_db):
    _closed("t-1", NOW.replace(hour=9), -12.5)
    assert "-$12.50" in _render()


def test_simulation_account_is_labelled(fresh_db):
    assert "Simulation" in _render(_FakeBridge(account={}))


def test_open_pnl_appears_only_when_something_is_open(fresh_db):
    assert "Open P&L:" not in _render()


def test_a_bridge_that_raises_still_produces_a_report(fresh_db):
    """The Balance button must answer while MT5 is down -- realised P&L comes
    from our own DB and does not need the broker at all."""
    class _Broken:
        async def get_account(self):
            raise RuntimeError("bridge down")

        async def get_tick(self):
            raise RuntimeError("bridge down")

    _closed("t-1", NOW.replace(hour=9), 30.0)
    text = _render(_Broken())
    assert "Simulation" in text
    assert "+$30.00" in text
