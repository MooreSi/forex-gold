"""Characterizes get_total_deposits/compute_mt5_performance/import_mt5_history
on SimulationEngine (core/engine.py) before task 020 extracts them -- see
docs/todo/refactor/core-mt5-history-migration/010-*.md.

All three call self._bridge (get_account/get_deal_history/get_positions),
so they need a real SimulationEngine.__new__(SimulationEngine) instance
with _bridge set manually to a fake test-double -- __init__ never runs, so
no live MT5 bridge/HTTP client is ever constructed. import_mt5_history also
calls self.pnl(...) (pack 1's already-extracted math, unchanged on
engine.py too), which needs no self state.
"""
import asyncio
import json
import os
import tempfile
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    _reset_thread_local_connection()
    os.remove(path)


class _FakeBridge:
    def __init__(self, account=None, deals=None, positions=None, raises=False):
        self._account   = account or {"balance": 1000.0, "equity": 1000.0}
        self._deals     = deals or []
        self._positions = positions or []
        self._raises    = raises
        self.deal_history_calls = 0

    async def get_account(self):
        if self._raises:
            raise RuntimeError("simulated bridge failure")
        return self._account

    async def get_deal_history(self, days: int = 7):
        self.deal_history_calls += 1
        if self._raises:
            raise RuntimeError("simulated bridge failure")
        return self._deals

    async def get_positions(self):
        if self._raises:
            raise RuntimeError("simulated bridge failure")
        return self._positions


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge()
    return e


def _deal(position_id=None, profit=0.0, entry=0, ts=None, volume=0.10,
          deal_type=0, price=2400.0, swap=0.0, fee=0.0, comment=""):
    return {
        "position_id": position_id, "profit": profit, "entry": entry,
        "time": ts if ts is not None else time.time(), "volume": volume,
        "type": deal_type, "price": price, "swap": swap, "fee": fee, "comment": comment,
    }


# ── get_total_deposits ────────────────────────────────────────────────────────

def test_get_total_deposits_sums_deposit_only_deals(fresh_db, engine):
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=None, profit=500.0),   # deposit
        _deal(position_id=None, profit=-100.0),  # withdrawal
        _deal(position_id=42, profit=25.0),       # a real trade deal -- excluded
    ])
    total = asyncio.run(TradingRuntime.get_total_deposits(engine))
    assert total == 400.0


def test_get_total_deposits_caches_for_an_hour(fresh_db, engine):
    bridge = _FakeBridge(deals=[_deal(position_id=None, profit=500.0)])
    engine._bridge = bridge
    first = asyncio.run(TradingRuntime.get_total_deposits(engine))
    assert first == 500.0
    assert bridge.deal_history_calls == 1

    second = asyncio.run(TradingRuntime.get_total_deposits(engine))
    assert second == 500.0
    assert bridge.deal_history_calls == 1  # cache hit -- bridge not called again


def test_get_total_deposits_returns_zero_on_bridge_error(fresh_db, engine):
    engine._bridge = _FakeBridge(raises=True)
    total = asyncio.run(TradingRuntime.get_total_deposits(engine))
    assert total == 0.0


# ── compute_mt5_performance ────────────────────────────────────────────────────

def test_compute_mt5_performance_win_rate_and_pnl(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(
        account={"balance": 1000.0, "equity": 1050.0},
        deals=[
            _deal(position_id=1, entry=0, ts=now - 3600, profit=0.0, volume=0.10),
            _deal(position_id=1, entry=1, ts=now - 1800, profit=100.0),
            _deal(position_id=2, entry=0, ts=now - 3600, profit=0.0, volume=0.10),
            _deal(position_id=2, entry=1, ts=now - 1800, profit=-50.0),
        ],
        positions=[],
    )
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf["closed_trades"] == 2
    assert perf["win_rate_pct"] == 50.0
    assert perf["source"] == "mt5"
    assert perf["balance"] == 1000.0
    assert perf["equity"] == 1050.0


def test_compute_mt5_performance_includes_open_positions_pnl(fresh_db, engine):
    engine._bridge = _FakeBridge(
        account={"balance": 1000.0, "equity": 1000.0},
        deals=[],
        positions=[{"profit": 30.0}, {"profit": -10.0}],
    )
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf["open_pnl"] == 20.0
    assert perf["open_trades"] == 2


def test_compute_mt5_performance_returns_empty_dict_on_bridge_error(fresh_db, engine):
    engine._bridge = _FakeBridge(raises=True)
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf == {}


# ── The numbers the Analysis page actually displays ────────────────────────────
#
# compute_mt5_performance returns 20-odd keys; the tests above pin five of them.
# frontend/pages/history reads eight MORE that nothing asserted:
# profit_factor, max_drawdown_pct, roi_pct and the five daily_* fields.
#
# docs/todo/refactor/frontend/restructure/phase2-view-decomposition/030-history.md
# leads its "What must NOT change" list with exactly these figures and says a
# characterization test pinned them in test_history_numbers_characterization.py.
# That file does not exist -- the premise is stale. These are that pin, placed
# here rather than in a new file so they reuse this module's _FakeBridge and
# engine fixtures instead of adding another copy of each.
#
# The expected values are derived by hand from the formulas in
# backend/src/services/broker/mt5_performance.py, not copied from a run:
#
#   trades, chronological           [+100, -50]
#   period start = balance - sum    1000 - 50            = 950
#   equity curve                    950 -> 1050 -> 1000
#   profit factor = wins / |losses| 100 / 50             = 2.0
#   max drawdown  = (peak-cum)/peak (1050-1000)/1050*100 = 4.76%
#   roi           = sum / start     50 / 950 * 100       = 5.26%


def _two_trade_bridge(now):
    """One +100 winner then one -50 loser, both closed inside the day."""
    return _FakeBridge(
        account={"balance": 1000.0, "equity": 1050.0},
        deals=[
            _deal(position_id=1, entry=0, ts=now - 3600, profit=0.0, volume=0.10),
            _deal(position_id=1, entry=1, ts=now - 1800, profit=100.0),
            _deal(position_id=2, entry=0, ts=now - 1700, profit=0.0, volume=0.10),
            _deal(position_id=2, entry=1, ts=now - 900,  profit=-50.0),
        ],
        positions=[],
    )


def test_profit_factor_is_gross_win_over_gross_loss(fresh_db, engine):
    engine._bridge = _two_trade_bridge(time.time())
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf["profit_factor"] == 2.0


def test_max_drawdown_is_measured_from_the_running_peak(fresh_db, engine):
    """Not from the period start, and not from the final balance.

    The comment in mt5_performance.py explains why the period start is clamped:
    a zero start makes any win-then-breakeven pair read as 100% drawdown.
    """
    engine._bridge = _two_trade_bridge(time.time())
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf["max_drawdown_pct"] == 4.76


def test_roi_is_measured_against_the_reconstructed_period_start(fresh_db, engine):
    """950, not the closing balance of 1000 -- otherwise ROI shrinks as the
    account grows, which reads as the strategy getting worse."""
    engine._bridge = _two_trade_bridge(time.time())
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf["roi_pct"] == 5.26


def test_the_daily_block_counts_only_todays_closes(fresh_db, engine):
    """The page's Daily P&L card. daily_pnl_24h is kept as an alias for the
    email scheduler and must stay equal to daily_pnl."""
    engine._bridge = _two_trade_bridge(time.time())
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf["daily_closed"] == 2
    assert perf["daily_pnl"] == 50.0
    assert perf["daily_pnl_24h"] == perf["daily_pnl"]
    assert perf["daily_win_rate_pct"] == 50.0
    assert perf["daily_best"] == 100.0
    assert perf["daily_worst"] == -50.0


def test_profit_factor_is_zero_when_there_are_no_losers(fresh_db, engine):
    """Deliberate: an all-winners period has no finite profit factor, and the
    code returns 0.0 rather than infinity. The page prints it as 0.00, which is
    misleading but is the behaviour being pinned, not endorsed."""
    now = time.time()
    engine._bridge = _FakeBridge(
        account={"balance": 1000.0, "equity": 1000.0},
        deals=[
            _deal(position_id=1, entry=0, ts=now - 3600, profit=0.0, volume=0.10),
            _deal(position_id=1, entry=1, ts=now - 1800, profit=100.0),
        ],
        positions=[],
    )
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    assert perf["profit_factor"] == 0.0


def test_every_key_the_analysis_page_reads_is_present(fresh_db, engine):
    """A renamed key would blank a card on the page and raise nothing.

    The page uses .get() with defaults throughout, so a key that disappears
    shows as 0.00 or --% rather than failing. That is the failure this guards.
    """
    engine._bridge = _two_trade_bridge(time.time())
    perf = asyncio.run(TradingRuntime.compute_mt5_performance(engine, days=90))
    for key in (
        "profit_factor", "max_drawdown_pct", "roi_pct",
        "daily_closed", "daily_pnl", "daily_win_rate_pct",
        "daily_best", "daily_worst",
    ):
        assert key in perf, f"frontend/pages/history reads {key!r} and it is gone"


# ── import_mt5_history ─────────────────────────────────────────────────────────

def test_import_mt5_history_no_deals_returns_error(fresh_db, engine):
    engine._bridge = _FakeBridge(deals=[])
    result = asyncio.run(TradingRuntime.import_mt5_history(engine, days=90))
    assert result["imported"] == 0
    assert "error" in result


def test_import_mt5_history_inserts_new_closed_position(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=555, entry=0, ts=now - 3600, deal_type=0,
              price=2400.0, volume=0.10, comment=""),
        _deal(position_id=555, entry=1, ts=now - 1800, deal_type=0,
              price=2410.0, profit=100.0, comment="tp hit"),
    ])
    result = asyncio.run(TradingRuntime.import_mt5_history(engine, days=90))
    assert result["imported"] == 1
    assert result["skipped"] == 0

    with db.db() as conn:
        row = db.row_to_dict(
            conn.execute("SELECT * FROM vantage_simulated_trades WHERE mt5_ticket=?", (555,)).fetchone()
        )
    assert row is not None
    assert row["direction"] == "BUY"
    assert row["exit_reason"] == "TP"
    assert row["status"] == "closed"


def test_import_mt5_history_skips_existing_ticket(fresh_db, engine):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            ("sig-1", "BUY", 2399.0, 2401.0, 2390.0, "closed", time.time()),
        )
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, mt5_ticket, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, status, "
            "open_time) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            ("t-1", "sig-1", 555, "BUY", 2399.0, 2401.0, 2400.0, 0.10, 0.0, 2390.0,
             "closed", time.time()),
        )
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=555, entry=0, ts=now - 3600, deal_type=0, price=2400.0),
        _deal(position_id=555, entry=1, ts=now - 1800, deal_type=0, price=2410.0, profit=100.0),
    ])
    result = asyncio.run(TradingRuntime.import_mt5_history(engine, days=90))
    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_import_mt5_history_skips_position_missing_open_or_close_deal(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=777, entry=0, ts=now - 3600, deal_type=0, price=2400.0),
        # no close deal (entry in (1,2,3)) for position 777
    ])
    result = asyncio.run(TradingRuntime.import_mt5_history(engine, days=90))
    assert result["imported"] == 0
    assert result["skipped"] == 1


def test_import_mt5_history_updates_sim_balance(fresh_db, engine):
    now = time.time()
    engine._bridge = _FakeBridge(deals=[
        _deal(position_id=555, entry=0, ts=now - 3600, deal_type=0, price=2400.0),
        _deal(position_id=555, entry=1, ts=now - 1800, deal_type=0, price=2410.0, profit=100.0),
    ])
    asyncio.run(TradingRuntime.import_mt5_history(engine, days=90))
    with db.db() as conn:
        balance = conn.execute("SELECT balance FROM vantage_simulation_account WHERE id=1").fetchone()[0]
    assert balance == 1100.0  # seeded 1000.0 + 100.0 imported profit
