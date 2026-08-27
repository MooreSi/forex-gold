"""Grid EA templates must reach the broker on arrival, not on zone re-entry.

A grid template stages real resting BuyLimit/SellLimit legs across the
signal's zone, so MT5 is what waits for price. Every path that instead held
the signal in Python until price came back and then opened at market threw
that away -- the legs were only placed once price had already arrived.

Covers the three routes that were still queueing (2026-07-30):
  * the pending-signal watcher (manual/sync/bot/ORB-added signals)
  * the Breakout Engine's zone handoff, which was a 1-point band
  * the Reversal Engine's LIMIT ORDER toggle, which dropped templates
"""
import asyncio
import os
import tempfile
import time

import pytest

from backend.src.services.broker import ea_templates as et
from backend.src.services.positions import core_grid_template_dispatch as gtd
from backend.src.services.signals import pending_activation as pa
from backend.src.db import database as db
from tests.conftest import remove_db_file


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
    pa._ACTIVATION_FAILURES.clear()
    yield db
    pa._ACTIVATION_FAILURES.clear()
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    remove_db_file(path)


class _Tick:
    """Price well ABOVE the BUY zone below -- i.e. nothing to fill yet."""
    ask = 4100.0
    bid = 4099.8
    spread_points = 20.0


def _queue_signal(source="Manual", direction="BUY", low=4060.0, high=4066.0,
                  sl=4055.0, signal_id="sig-1"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,tp1,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, source, direction, low, high, sl, high + 20.0,
             "pending", time.time()),
        )


def _assign(source: str, template: str, mode: str):
    et.save_ea_template(template, {"mode": mode})
    db.set_channel_strategy_override(source, et.override_for_template(template))


def _run_watcher(monkeypatch, dpm_candles=None):
    """Drive one watcher cycle; returns the signal_ids it tried to open."""
    opened: list[str] = []

    async def _open(bridge, signal_id, **kw):
        opened.append(signal_id)
        with db.db() as conn:
            conn.execute("UPDATE vantage_signals SET status='activated' "
                         "WHERE signal_id=?", (signal_id,))
        return {"entry_price": 0.0, "trade_id": "t" * 16}

    monkeypatch.setattr(pa, "open_trade_from_signal", _open)
    monkeypatch.setattr(pa.telegram_alerts, "send_message",
                        lambda *a, **kw: asyncio.sleep(0))
    rs = {"max_open_trades": 10, "trade_strategy": "scale_out"}
    asyncio.run(pa.try_activate_pending_signals(
        _Tick(), rs, object(), {}, dpm_candles or []))
    return opened


# ── the shared lookup ────────────────────────────────────────────────────

def test_grid_template_recognised_from_its_override(fresh_db):
    et.save_ea_template("GridT", {"mode": "grid"})
    assert gtd.grid_template(et.override_for_template("GridT")) is not None


def test_single_mode_template_is_not_a_grid(fresh_db):
    """Single mode really is a market-fill strategy -- it must keep queueing."""
    et.save_ea_template("SingleT", {"mode": "single"})
    assert gtd.grid_template(et.override_for_template("SingleT")) is None


def test_builtin_strategy_is_not_a_grid(fresh_db):
    assert gtd.grid_template("scale_out") is None
    assert gtd.grid_template(None) is None


def test_deleted_template_resolves_to_none(fresh_db):
    assert gtd.grid_template("template:NoSuchTemplate") is None


def test_source_lookup_resolves_through_the_channel_override(fresh_db):
    _assign("Reversal Engine", "GridT", "grid")
    assert gtd.grid_template_for_source("Reversal Engine") is not None
    assert gtd.grid_template_for_source("Breakout Engine") is None
    assert gtd.grid_template_for_source(None) is None


def test_auto_override_is_never_a_template(fresh_db):
    db.set_channel_strategy_override("Reversal Engine", "auto", auto=True)
    assert gtd.grid_template_for_source("Reversal Engine") is None


# ── PendingWatcher: place on arrival ─────────────────────────────────────

def test_grid_signal_is_placed_while_price_is_outside_the_zone(monkeypatch, fresh_db):
    """The whole point: the resting legs ARE the wait, so the watcher must
    not require price to be back in the zone before staging them."""
    _assign("Manual", "GridT", "grid")
    _queue_signal(source="Manual")
    assert _run_watcher(monkeypatch) == ["sig-1"]


def test_single_mode_template_still_waits_for_the_zone(monkeypatch, fresh_db):
    _assign("Manual", "SingleT", "single")
    _queue_signal(source="Manual")
    assert _run_watcher(monkeypatch) == []


def test_builtin_strategy_still_waits_for_the_zone(monkeypatch, fresh_db):
    _queue_signal(source="Manual")
    assert _run_watcher(monkeypatch) == []


def test_grid_signal_is_not_deferred_by_a_contrary_m5_candle(monkeypatch, fresh_db):
    """Momentum confirmation asks whether the move INTO the zone looks real.
    Price hasn't reached the zone yet, so there is nothing to confirm --
    deferring here would just reinstate the Python-side wait."""
    _assign("Manual", "GridT", "grid")
    _queue_signal(source="Manual", direction="BUY")
    bearish = [{"open": 4100.0, "close": 4090.0}]
    assert _run_watcher(monkeypatch, dpm_candles=bearish) == ["sig-1"]


def test_grid_signal_still_respects_the_max_trades_cap(monkeypatch, fresh_db):
    """Placing on arrival must not become a way around the exposure cap."""
    _assign("Manual", "GridT", "grid")
    _queue_signal(source="Manual")
    opened: list[str] = []

    async def _open(bridge, signal_id, **kw):
        opened.append(signal_id)
        return {"entry_price": 0.0}

    monkeypatch.setattr(pa, "open_trade_from_signal", _open)
    monkeypatch.setattr(pa, "get_open_trades", lambda: [{"trade_id": "x"}])
    monkeypatch.setattr(pa.telegram_alerts, "send_message",
                        lambda *a, **kw: asyncio.sleep(0))
    asyncio.run(pa.try_activate_pending_signals(
        _Tick(), {"max_open_trades": 1, "trade_strategy": "scale_out"},
        object(), {}, []))
    assert opened == []


def test_grid_signal_still_expires_on_its_own_window(monkeypatch, fresh_db):
    """A grid that could not be placed (EA down, cap reached) must not become
    immortal just because it no longer waits for the zone."""
    _assign("Manual", "GridT", "grid")
    _queue_signal(source="Manual")
    with db.db() as conn:
        conn.execute("UPDATE vantage_signals SET created_at=? WHERE signal_id='sig-1'",
                     (time.time() - pa._TEMPLATE_PENDING_EXPIRY_SEC - 1,))
    assert _run_watcher(monkeypatch) == []
    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id='sig-1'").fetchone()[0]
    assert status == "expired"


# ── Breakout Engine: a zone the legs can actually spread across ──────────

def test_breakout_grid_zone_spans_the_retest(fresh_db):
    from backend.src.services.breakout_signal.breakout_signal_live_execute import _grid_zone
    _assign("Breakout Engine", "GridT", "grid")
    sig = {"broken_level": 4090.0}
    assert _grid_zone(sig, "BUY", 4100.0) == (4090.0, 4100.0)
    assert _grid_zone({"broken_level": 4110.0}, "SELL", 4100.0) == (4100.0, 4110.0)


def test_breakout_zone_stays_the_default_band_without_a_grid_template(fresh_db):
    from backend.src.services.breakout_signal.breakout_signal_live_execute import _grid_zone
    assert _grid_zone({"broken_level": 4090.0}, "BUY", 4100.0) == (4099.5, 4100.5)


def test_breakout_grid_zone_falls_back_when_the_level_is_unusable(fresh_db):
    """A sweep can leave the level on the wrong side of the entry, and a
    level right at the market gives no room -- both must fall back rather
    than emit an inverted or degenerate zone."""
    from backend.src.services.breakout_signal.breakout_signal_live_execute import _grid_zone
    _assign("Breakout Engine", "GridT", "grid")
    assert _grid_zone({"broken_level": 4105.0}, "BUY", 4100.0) == (4099.5, 4100.5)
    assert _grid_zone({"broken_level": 4099.8}, "BUY", 4100.0) == (4099.5, 4100.5)
    assert _grid_zone({}, "BUY", 4100.0) == (4099.5, 4100.5)


# ── Reversal Engine: the LIMIT ORDER toggle must not drop the template ───

# ── creation-time staging (both generators) ──────────────────────────────

@pytest.fixture
def re_repo_db():
    from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    yield re_repo
    remove_db_file(path)


@pytest.fixture
def bo_repo_db():
    from backend.src.services.breakout_signal import breakout_signal_repo as bo_repo
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    bo_repo.init(path)
    yield bo_repo
    remove_db_file(path)


def _re_mixin(calls):
    from backend.src.services.reversal_engine.reversal_engine_live_execute import _LiveExecuteMixin
    mixin = _LiveExecuteMixin()

    async def _fake_exec(sig, price, tick):
        calls.append(sig)

    mixin._try_live_execute = _fake_exec
    return mixin


def _bo_mixin(calls):
    from backend.src.services.breakout_signal.breakout_signal_live_execute import _LiveExecuteMixin
    mixin = _LiveExecuteMixin()

    async def _fake_exec(sig, price, tick):
        calls.append(sig)

    mixin._execute_live = _fake_exec
    return mixin


def test_re_stages_a_grid_at_signal_creation(fresh_db, re_repo_db):
    """Not at zone entry: the resting legs are what waits for the zone, so
    holding them until price arrives spends the whole advantage."""
    _assign("Reversal Engine", "GridT", "grid")
    db.update_risk_settings({"re_live_execution": 1})
    sig_id = re_repo_db.create_signal({"direction": "BUY", "signal_ref": "RE-1",
                                       "entry_low": 4060.0, "entry_high": 4066.0})
    calls: list = []
    assert asyncio.run(_re_mixin(calls)._maybe_stage_grid_template(sig_id, None, 4100.0))
    assert len(calls) == 1


def test_re_does_not_stage_without_a_grid_template(fresh_db, re_repo_db):
    db.update_risk_settings({"re_live_execution": 1})
    sig_id = re_repo_db.create_signal({"direction": "BUY", "signal_ref": "RE-1"})
    calls: list = []
    assert not asyncio.run(_re_mixin(calls)._maybe_stage_grid_template(sig_id, None, 4100.0))
    assert calls == []


def test_re_does_not_stage_in_virtual_mode(fresh_db, re_repo_db):
    """Live execution off means the engine is modelling, not trading."""
    _assign("Reversal Engine", "GridT", "grid")
    db.update_risk_settings({"re_live_execution": 0})
    sig_id = re_repo_db.create_signal({"direction": "BUY", "signal_ref": "RE-1"})
    calls: list = []
    assert not asyncio.run(_re_mixin(calls)._maybe_stage_grid_template(sig_id, None, 4100.0))
    assert calls == []


def test_re_does_not_stage_a_signal_already_dispatched(fresh_db, re_repo_db):
    """vantage_signal_id is the durable "already placed" marker -- staging
    twice would put a second grid on the same setup."""
    _assign("Reversal Engine", "GridT", "grid")
    db.update_risk_settings({"re_live_execution": 1})
    sig_id = re_repo_db.create_signal({"direction": "BUY", "signal_ref": "RE-1"})
    re_repo_db.update_live_exec(sig_id, vantage_sig_id="vsig-1", status="executed")
    calls: list = []
    assert not asyncio.run(_re_mixin(calls)._maybe_stage_grid_template(sig_id, None, 4100.0))
    assert calls == []


def test_bo_stages_a_grid_at_signal_creation(monkeypatch, fresh_db, bo_repo_db):
    _assign("Breakout Engine", "GridT", "grid")
    db.update_risk_settings({"bo_live_execution": 1})
    monkeypatch.setattr(db, "is_session_allowed", lambda: (True, "London"))
    sig_id = bo_repo_db.create_signal({"direction": "BUY", "signal_ref": "BO-1",
                                       "breakout_type": "break", "entry_mid": 4100.0,
                                       "stop_loss": 4090.0})
    calls: list = []
    assert asyncio.run(_bo_mixin(calls)._maybe_stage_grid_template(sig_id, 4100.0, None))
    assert len(calls) == 1


def test_bo_staging_respects_the_session_gate(monkeypatch, fresh_db, bo_repo_db):
    """Same gate _check_outcomes applies before triggering -- placing on
    arrival must not become a way to trade a switched-off session."""
    _assign("Breakout Engine", "GridT", "grid")
    db.update_risk_settings({"bo_live_execution": 1})
    monkeypatch.setattr(db, "is_session_allowed", lambda: (False, "Sydney"))
    sig_id = bo_repo_db.create_signal({"direction": "BUY", "signal_ref": "BO-1",
                                       "breakout_type": "break", "entry_mid": 4100.0,
                                       "stop_loss": 4090.0})
    calls: list = []
    assert not asyncio.run(_bo_mixin(calls)._maybe_stage_grid_template(sig_id, 4100.0, None))
    assert calls == []


def test_bo_does_not_stage_a_signal_already_dispatched(monkeypatch, fresh_db, bo_repo_db):
    _assign("Breakout Engine", "GridT", "grid")
    db.update_risk_settings({"bo_live_execution": 1})
    monkeypatch.setattr(db, "is_session_allowed", lambda: (True, "London"))
    sig_id = bo_repo_db.create_signal({"direction": "BUY", "signal_ref": "BO-1",
                                       "breakout_type": "break", "entry_mid": 4100.0,
                                       "stop_loss": 4090.0})
    bo_repo_db.update_live_exec_result(sig_id, None, "vsig-1", "success")
    calls: list = []
    assert not asyncio.run(_bo_mixin(calls)._maybe_stage_grid_template(sig_id, 4100.0, None))
    assert calls == []


class _FakeEA:
    """Healthy EA that records any pending order it is asked to place."""

    def __init__(self):
        self.calls = []

    def is_ea_healthy(self):
        return True

    async def place_pending_order(self, *a, **kw):
        self.calls.append((a, kw))
        return {"type": "pending_order_placed", "ticket": 1}


def _run_re_limit_order(monkeypatch):
    from backend.src.services.broker import ea_bridge as ea_mod
    from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
    from backend.src.services.reversal_engine.reversal_engine_live_execute import _LiveExecuteMixin
    # The no-template control reaches re_db.update_live_exec, which needs the
    # engine's own database initialised.
    fd, re_path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(re_path)
    fake = _FakeEA()
    monkeypatch.setattr(ea_mod, "get_instance", lambda: fake)
    try:
        result = asyncio.run(_LiveExecuteMixin()._try_re_limit_order(
            {"id": 1, "signal_ref": "RE-1"}, "vsig-1", None))
    finally:
        # The engine's own database, opened under the reversal_engine
        # namespace. remove_db_file closes every namespace first; a bare
        # os.remove leaves that adapter holding the file on Windows.
        remove_db_file(re_path)
    return result, fake


def test_re_limit_order_toggle_defers_to_grid_staging(monkeypatch, fresh_db):
    """place_pending_order carries no template payload and the EA forces
    isTemplate=false on that path, so routing a grid template through it
    placed one plain limit and lost the grid entirely. Returning None hands
    the signal back to the staging path -- and nothing may reach the EA's
    single-limit call on the way."""
    _assign("Reversal Engine", "GridT", "grid")
    result, fake = _run_re_limit_order(monkeypatch)
    assert result is None
    assert fake.calls == []


def test_re_limit_order_still_runs_without_a_grid_template(monkeypatch, fresh_db):
    """Control for the test above: with no grid template assigned the
    function must get past the bypass (it stops later, on this bare mixin's
    missing bridge -- reported as handled, not as 'EA unreachable')."""
    result, fake = _run_re_limit_order(monkeypatch)
    assert result is True
    assert fake.calls == []
