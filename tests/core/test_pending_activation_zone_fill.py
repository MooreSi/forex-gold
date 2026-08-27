"""Why queued signals sat in 'pending' and never activated.

Reported live 2026-08-27: signals from every channel were being queued and
then "nothing happens with them -- they don't activate when the price goes
within range". Three separate causes, all pinned here.

`open_trade_from_signal` is mocked in every test: no MT5 order is placed,
closed or modified anywhere in this file.
"""
from __future__ import annotations

import asyncio
import time
import types
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.channels import repo as ch_repo
from backend.src.services.positions import monitor_cycle as mc
from backend.src.services.signals import pending_activation as psa


# ── 1. The frozen momentum candle ────────────────────────────────────────────
#
# The pending watcher defers any signal whose direction disagrees with the
# last M5 candle it is handed. That candle comes from a plain list on the
# runtime which the monitor cycle only ever refreshed while a trade was
# open, and which nothing ever cleared -- so once the last position closed
# it froze at whatever bar was current then. A frozen bearish bar deferred
# every queued BUY, on every cycle, until it expired.

def _monitor_ctx(trades, candles, *, fetched, set_calls, candles_raise=False):
    async def get_tick():
        return types.SimpleNamespace(bid=2400.0, ask=2400.2)

    async def _noop(*a, **k):
        return None

    async def get_candles(*a, **k):
        fetched.append(a)
        if candles_raise:
            raise RuntimeError("bridge down")
        return [{"open": 2400.0, "close": 2401.0}]

    return mc.MonitorCtx(
        bridge=object(), cfg={}, tp_trigger_cache={}, dpm_cache={},
        scale_out_last_fail={}, pending_activation_retry_after={},
        get_dpm_candles=lambda: candles,
        set_dpm_candles=lambda v: set_calls.append(v),
        get_tick=get_tick, get_open_trades=lambda: trades,
        get_candles=get_candles, is_trading_paused=lambda: False,
        background_open_commentary=_noop, close_full_after_tps=_noop,
        make_close_trade_ctx=lambda: object(),
        sync_closed_mt5_positions=_noop, close_trade=_noop,
    )


@pytest.fixture
def quiet_cycle(monkeypatch):
    async def _none(*a, **k):
        return None
    for attr in ("_check_equity_protect_impl", "_check_basket_harvest_impl",
                 "_reconcile_orphaned_trades_impl", "_repair_template_placeholders_impl",
                 "_profit_sweep_impl", "_run_dpm_calibration_impl",
                 "_ime_timeout_watchdog_impl", "_revalidate_pending_impl"):
        if hasattr(mc, attr):
            monkeypatch.setattr(mc, attr, _none)
    seen = []

    async def _watch(tick, rs, bridge, retry_after, dpm_candles, **kw):
        seen.append(dpm_candles)
        return False
    monkeypatch.setattr(mc, "_try_activate_pending_signals_impl", _watch)
    return seen


def test_the_candle_cache_is_refreshed_before_the_pending_watcher_reads_it(
    fresh_db, quiet_cycle,
):
    """With no trade open the cache was never refreshed, so the watcher
    scored today's signal against a bar from whenever the last position
    happened to close."""
    db.update_risk_settings({"dpm_enabled": 1, "auto_execute_signals": 1})
    fetched, set_calls = [], []
    stale = [{"open": 2500.0, "close": 2490.0}]

    asyncio.run(mc.run_monitor_cycle(_monitor_ctx([], stale, fetched=fetched, set_calls=set_calls)))

    assert fetched, "no candle fetch happened at all"
    assert set_calls and set_calls[-1] != stale


def test_a_failed_candle_fetch_clears_the_cache_rather_than_keeping_a_stale_bar(
    fresh_db, quiet_cycle,
):
    db.update_risk_settings({"dpm_enabled": 1, "auto_execute_signals": 1})
    fetched, set_calls = [], []
    stale = [{"open": 2500.0, "close": 2490.0}]

    asyncio.run(mc.run_monitor_cycle(
        _monitor_ctx([], stale, fetched=fetched, set_calls=set_calls, candles_raise=True)))

    assert set_calls == [[]], "a dead bridge must not leave the previous bar in place"


def test_no_candle_fetch_when_the_watcher_is_off_and_nothing_is_open(fresh_db, quiet_cycle):
    """The refresh exists to serve a reader. With auto-execution off and no
    open trade there is nobody to serve, so it must not cost a bridge call."""
    db.update_risk_settings({"dpm_enabled": 1, "auto_execute_signals": 0})
    fetched, set_calls = [], []

    asyncio.run(mc.run_monitor_cycle(_monitor_ctx([], [], fetched=fetched, set_calls=set_calls)))

    assert fetched == []


# ── 2. The strategy a queued signal is scored under ──────────────────────────

_TICK = types.SimpleNamespace(bid=2400.0, ask=2400.5)


def _insert_signal(sig_id="sig-1", source="Telegram Auto (Gold Diggers Scalping)",
                   created_at=None):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, tp1, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (sig_id, source, "BUY", 2399.0, 2401.0, 2390.0, 2410.0, "pending",
             created_at if created_at is not None else time.time()),
        )


def _status(sig_id="sig-1"):
    with db.db() as conn:
        return conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id=?", (sig_id,)).fetchone()[0]


def _run_watcher(rs=None):
    rs = rs or {"max_open_trades": 1, "trade_strategy": "scale_out"}
    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal", new=mock.AsyncMock()) as ot:
        asyncio.run(psa.try_activate_pending_signals(_TICK, rs, object(), {}, []))
    return ot


def test_a_channel_override_survives_the_decorated_source_name(fresh_db):
    """Signal rows are stamped "Telegram Auto (<channel>)"; the override is
    stored under the bare name. Only the two channels with an explicit
    entry in CANONICAL_CHANNELS resolved -- any third channel's Channel
    Strategy pick was silently ignored, so it got the shortest expiry
    window instead of Reversal Runner's four hours."""
    ch_repo.set_channel_strategy_override("Gold Diggers Scalping", "reversal_runner")
    _insert_signal(created_at=time.time() - 600)   # 10 min: past every short window

    ot = _run_watcher()

    assert _status() != "expired", "expired inside Reversal Runner's 4h window"
    assert ot.called, "price was in the zone -- it should have activated"


def test_a_channel_left_on_auto_is_scored_under_what_auto_actually_picked(fresh_db):
    """`effective_strategy` was `override or global`, so a channel on Auto --
    the default -- had the literal string "auto" measured against the expiry
    ladder: never a runner, never a template, always the short default."""
    ch_repo.set_channel_strategy_override("Gold Diggers Scalping", None, auto=True)
    db.set_channel_strategy_rec("Gold Diggers Scalping", "reversal_runner", "test", 0.9)
    _insert_signal(created_at=time.time() - 600)

    ot = _run_watcher()

    assert _status() != "expired"
    assert ot.called


# ── 3. The R:R gate a queued signal faces ────────────────────────────────────

def test_the_queued_and_fresh_paths_share_one_r_r_bypass_list(fresh_db):
    """The pending watcher kept its own copy of "this strategy's own levels
    replace the signal's, so scoring the signal's R:R is meaningless". It had
    drifted: no Scalp Runner, no EA Templates, no IME exemption. The same
    signal that executed on arrival was then refused on zone re-entry and
    could only sit until it expired."""
    from backend.src.services.trading.scan_auto_execute import (
        _PRE_TRADE_FILTER_BYPASS_STRATEGIES as fresh_path,
    )
    assert psa._PRE_TRADE_FILTER_BYPASS_STRATEGIES is fresh_path


def test_a_scalp_runner_signal_is_not_refused_on_zone_re_entry(fresh_db):
    """Scalp Runner replaces the signal's stop with its own fixed point
    distance, so a check_pre_trade_filters R:R measured against the signal's
    numbers declines a trade on levels the strategy never uses."""
    from backend.src.utils.models import STRATEGY_SCALP_RUNNER
    ch_repo.set_channel_strategy_override("Gold Diggers Scalping", STRATEGY_SCALP_RUNNER)
    # TP1 barely past the entry against a wide stop -- a hopeless R:R on the
    # signal's own numbers, and irrelevant to how Scalp Runner will run it.
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, tp1, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("sig-rr", "Telegram Auto (Gold Diggers Scalping)", "BUY", 2399.0, 2401.0,
             2380.0, 2400.6, "pending", time.time()),
        )

    ot = _run_watcher()

    assert ot.called, "refused on an R:R measured against a stop it will not use"
