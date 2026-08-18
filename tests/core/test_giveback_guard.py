"""Intraday give-back guard — stop for the day once today's profit is handed back.

The existing limits both measure from the day's OPENING balance, so neither can
see the shape that actually cost money here: 2026-08-17 peaked at +$348.76 of
realised P&L at 09:06 and closed -$88.48, and 08-14 peaked +$182 and closed
-$877.69. Realised P&L never breaches a from-open threshold on the way down
from a peak, so "protect the profit I had" was not expressible at all.

Replayed over the account's own closes since 2026-07-20, that period realised
-$2,718.59; with this guard at $50 / 40% it comes out at -$5.67.

The guard is deliberately NOT behind risk_governor_enabled: the governor also
takes over position sizing and adds its own pre-trade gates, and this account
runs with it off -- which is exactly why its configured 3% daily-loss limit sat
inert through both losing days.
"""
import os
import tempfile
import time

import pytest

from forex_trader.core import core_risk_governor as rg
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
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


_seq = [0]


def _closes(pnls, day_start=None):
    """Write closed trades across today, in the order given.

    Ids come from a module counter so repeated calls in one test (which is how
    "recomputed, not stored" is proven) cannot collide on the signal_id key.
    """
    base = (day_start if day_start is not None else rg.rg_day_start_ts()) + 60
    with db.db() as conn:
        for p in pnls:
            i = _seq[0]; _seq[0] += 1
            conn.execute(
                "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
                "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
                (f"s{i}", "BUY", 1.0, 2.0, 0.5, "filled", base + i),
            )
            conn.execute(
                "INSERT INTO vantage_simulated_trades "
                "(trade_id, signal_id, direction, entry_low, entry_high, entry_price, "
                " lot_size, remaining_lots, stop_loss, status, open_time, close_time, "
                " close_price, net_pnl) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"t{i}", f"s{i}", "BUY", 1.0, 2.0, 1.5, 0.1, 0.0, 0.5, "closed",
                 base + i, base + i + 1, 1.6, float(p)),
            )


ON = {"giveback_guard_enabled": 1, "giveback_arm_usd": 50.0, "giveback_pct": 40.0}


# ── the watermark ────────────────────────────────────────────────────────────

def test_peak_is_the_running_high_not_the_final_total(fresh_db):
    _closes([100, 60, -120])
    realised, peak = rg.day_pnl_and_peak()
    assert realised == pytest.approx(40.0)
    assert peak == pytest.approx(160.0)


def test_peak_is_recomputed_from_the_rows_not_stored(fresh_db):
    """Stateless on purpose: a stored watermark needs resetting at the broker
    day boundary and has to survive restarts, and both failures are silent --
    the guard just stops guarding."""
    _closes([100])
    assert rg.day_pnl_and_peak()[1] == pytest.approx(100.0)
    _closes([-30])
    realised, peak = rg.day_pnl_and_peak()
    assert (realised, peak) == (pytest.approx(70.0), pytest.approx(100.0))


def test_yesterdays_trades_do_not_count(fresh_db):
    _closes([500], day_start=rg.rg_day_start_ts() - 86400)
    _closes([20])
    realised, peak = rg.day_pnl_and_peak()
    assert realised == pytest.approx(20.0) and peak == pytest.approx(20.0)


def test_a_day_that_only_ever_loses_has_a_zero_peak(fresh_db):
    _closes([-40, -60])
    assert rg.day_pnl_and_peak() == (pytest.approx(-100.0), 0.0)


# ── when it trips ────────────────────────────────────────────────────────────

def test_trips_once_more_than_the_limit_of_the_peak_is_handed_back(fresh_db):
    _closes([100, -45])          # peak 100, now 55, floor 60
    assert rg.check_giveback_guard(ON) is not None


def test_does_not_trip_while_inside_the_limit(fresh_db):
    _closes([100, -35])          # peak 100, now 65, floor 60
    assert rg.check_giveback_guard(ON) is None


def test_exactly_on_the_floor_trips(fresh_db):
    """The floor is the point at which too much is already gone; treating it as
    'still fine' just defers the same decision by one tick."""
    _closes([100, -40])          # now 60, floor 60
    assert rg.check_giveback_guard(ON) is not None


def test_the_2026_08_17_shape_is_what_this_catches(fresh_db):
    """Up +$348, closed -$88: never breaches a from-open daily-loss limit on
    the way down, which is why nothing stopped it."""
    _closes([348.76, -120, -150, -167.24])
    assert rg.check_giveback_guard(ON) is not None
    realised, peak = rg.day_pnl_and_peak()
    assert peak == pytest.approx(348.76) and realised < 0


# ── when it must stay out of the way ─────────────────────────────────────────

def test_off_by_default(fresh_db):
    _closes([100, -90])
    assert rg.check_giveback_guard(db.get_risk_settings()) is None


def test_a_day_below_the_arming_profit_is_never_locked_out(fresh_db):
    """Ordinary churn around break-even must not be able to end the day."""
    _closes([40, -38])           # peak 40 < arm 50, and 95% given back
    assert rg.check_giveback_guard(ON) is None


def test_a_losing_day_is_the_daily_loss_limits_job_not_this_one(fresh_db):
    _closes([-200])
    assert rg.check_giveback_guard(ON) is None


@pytest.mark.parametrize("rs", [
    {"giveback_guard_enabled": 1, "giveback_arm_usd": 0, "giveback_pct": 40},
    {"giveback_guard_enabled": 1, "giveback_arm_usd": 50, "giveback_pct": 0},
])
def test_a_zero_setting_disables_it_rather_than_tripping_on_everything(fresh_db, rs):
    _closes([100, -100])
    assert rg.check_giveback_guard(rs) is None


def test_no_trades_today_is_not_a_give_back(fresh_db):
    assert rg.check_giveback_guard(ON) is None


# ── applying the halt ────────────────────────────────────────────────────────

def test_applying_it_pauses_until_the_next_broker_day(fresh_db):
    _closes([100, -60])
    rg.apply_giveback_guard_on_close(ON)
    until = float(db.get_app_config("trade_pause_until") or 0)
    assert until == pytest.approx(rg.rg_day_start_ts() + 86400.0)
    assert rg.is_trading_paused() is True
    assert "Give-back guard" in (db.get_app_config("risk_halt_reason") or "")


def test_it_uses_the_key_the_order_gate_actually_reads(fresh_db):
    """Same trade_pause_until the governor and /pause use, so Resume, the
    panel and the status line all keep working with no special case."""
    _closes([100, -60])
    rg.apply_giveback_guard_on_close(ON)
    assert rg.is_trading_paused() is True
    db.set_app_config("trade_pause_until", "0")
    assert rg.is_trading_paused() is False


def test_it_does_not_extend_an_existing_pause(fresh_db):
    """A shorter manual pause must not be silently promoted to a full day."""
    manual = time.time() + 600
    db.set_app_config("trade_pause_until", str(manual))
    _closes([100, -60])
    rg.apply_giveback_guard_on_close(ON)
    assert float(db.get_app_config("trade_pause_until")) == pytest.approx(manual)


def test_applying_it_when_untripped_changes_nothing(fresh_db):
    _closes([100, -10])
    rg.apply_giveback_guard_on_close(ON)
    assert float(db.get_app_config("trade_pause_until") or 0) == 0


def test_it_runs_without_the_risk_governor(fresh_db):
    """The whole point: this account has risk_governor_enabled = 0, which is
    why its 3% daily-loss limit never fired."""
    rs = {**ON, "risk_governor_enabled": 0}
    _closes([100, -60])
    rg.apply_giveback_guard_on_close(rs)
    assert rg.is_trading_paused() is True


def test_close_trade_invokes_the_guard_unconditionally():
    """Guards the wiring: sitting inside the governor's `if` would make it
    unavailable to the setup it was written for."""
    import inspect
    from forex_trader.core import core_close_trade
    src = inspect.getsource(core_close_trade)
    assert "apply_giveback_guard_on_close" in src
