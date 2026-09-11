"""A queued signal that survived a blind gap must be re-validated before it
can execute.

The gap: while trading is paused -- a circuit breaker, the daily-loss halt,
a manual pause -- monitor_cycle skips the pending-signal watcher entirely
(`if _pending_watch and not is_trading_paused`). Nothing in the queue ages,
expires or is re-checked for the whole halt. The moment the pause lifts, the
next cycle runs the whole backlog at once, and any signal still inside its
strategy's expiry window (1h for an EA template, 4h for the runner
strategies) activates on the first tick that touches its zone.

The same hole opens whenever the watcher cannot run for any other reason:
auto-execute toggled off and back on, or an app restart. One mechanism
covers all three -- the watcher notices its own absence.

What a stale signal must then prove (owner, 2026-09-11): the R:R filter it
would have faced as a fresh signal, scored against the CURRENT price, with
the Immediate Market Entry bypass suspended. IME exists because the user
opted into taking a channel's fill "the moment the signal lands"; a signal
that sat through a halt is not that signal any more, so the reason for the
bypass has gone. The static RR_BYPASS_SOURCES channels bypass for a
different reason -- they supply their own levels -- and keep their
exemption.

NO real or demo MT5 order is placed, closed or modified by this file:
open_trade_from_signal is a mock in every test that reaches it, and no
bridge here does anything but exist.
"""
import asyncio
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.risk import governor as rg
from backend.src.services.signals import gap_revalidation as gr
from backend.src.services.signals import pending_activation as psa
from tests._fakes import _FakeBridge


# BUY 2399-2401, stop 2390, TP1 2402 -- 2 points of reward against 10 of
# risk from a live 2400.0 bid: 0.20:1, well under the 0.75:1 floor. Price is
# inside the zone, so no gap-fire is involved.
_TICK = SimpleNamespace(bid=2400.0, ask=2400.5)
_IME_RS = {"max_open_trades": 1, "trade_strategy": "scale_out",
           "immediate_market_entry": 1}


@pytest.fixture(autouse=True)
def _clean_module_state():
    gr._STALE.clear()
    psa._ACTIVATION_FAILURES.clear()
    yield
    gr._STALE.clear()
    psa._ACTIVATION_FAILURES.clear()


def _insert_signal(sig_id="sig-1", source="GOLD DIGGERS INSTITUTIONAL"):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, "
            "entry_low, entry_high, stop_loss, tp1, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (sig_id, source, "BUY", 2399.0, 2401.0, 2390.0, 2402.0,
             "pending", time.time()),
        )


def _configure_ime_channel(name="GOLD DIGGERS INSTITUTIONAL"):
    db.save_channel_parser_config(name, "gd2", "", True, True, "test")
    db.update_risk_settings({"immediate_market_entry": 1})
    db._rs_cache = None
    db._rs_cache_ts = 0.0


# ── The gap detector ─────────────────────────────────────────────────────────

def test_first_run_on_a_fresh_install_reports_no_gap(fresh_db):
    """Nothing has ever been recorded, so nothing can be said about how long
    the watcher was away. Marking the queue stale on the strength of an
    absent record would make every fresh install distrust its first signal."""
    gap = gr.observe_watcher_run(time.time(), ["sig-1"])
    assert gap == 0.0
    assert gr.needs_revalidation("sig-1") is False


def test_normal_cadence_marks_nothing(fresh_db):
    """The watcher runs every 1-5s. Two consecutive cycles are not a gap."""
    now = time.time()
    gr.observe_watcher_run(now, [])
    gr.observe_watcher_run(now + 2.0, ["sig-1"])
    assert gr.needs_revalidation("sig-1") is False


def test_a_long_absence_marks_every_queued_signal(fresh_db):
    """A ten-minute halt. Both signals were in the queue when the watcher
    came back, so both are judged stale -- the one that predates the halt
    and the one that arrived during it are equally unexamined."""
    now = time.time()
    gr.observe_watcher_run(now - 600, [])
    gap = gr.observe_watcher_run(now, ["sig-1", "sig-2"])
    assert gap == pytest.approx(600, abs=20)
    assert gr.needs_revalidation("sig-1") is True
    assert gr.needs_revalidation("sig-2") is True


def test_a_signal_queued_after_the_gap_is_not_marked(fresh_db):
    """The mark is not a mode. A signal that arrives once the watcher is
    running again was never unexamined, and must keep the fresh-signal
    treatment."""
    now = time.time()
    gr.observe_watcher_run(now - 600, [])
    gr.observe_watcher_run(now, ["sig-1"])
    gr.observe_watcher_run(now + 2.0, ["sig-1", "sig-later"])
    assert gr.needs_revalidation("sig-later") is False


def test_clear_drops_the_mark(fresh_db):
    now = time.time()
    gr.observe_watcher_run(now - 600, [])
    gr.observe_watcher_run(now, ["sig-1"])
    gr.clear("sig-1")
    assert gr.needs_revalidation("sig-1") is False


def test_the_gap_survives_a_restart(fresh_db):
    """Module state does not survive a restart, so the last-run time is
    recorded in the database. An app that was down for ten minutes must
    reach the same conclusion as one that was paused for ten minutes."""
    now = time.time()
    gr.observe_watcher_run(now - 600, [])
    gr._STALE.clear()          # a restart: in-memory state is gone, the DB is not
    gr.observe_watcher_run(now, ["sig-1"])
    assert gr.needs_revalidation("sig-1") is True


# ── The R:R bypass, suspended ────────────────────────────────────────────────

def test_ime_bypass_is_suspended_for_a_stale_signal(fresh_db):
    """With IME on, every configured Telegram channel skips the R:R filter.
    A stale signal is no longer an immediate entry, so it does not."""
    _configure_ime_channel()
    bypassed = rg.check_pre_trade_filters(
        "BUY", 2399.0, 2401.0, 2390.0, tp1=2402.0, actual_price=2400.0,
        source_name="GOLD DIGGERS INSTITUTIONAL",
    )
    enforced = rg.check_pre_trade_filters(
        "BUY", 2399.0, 2401.0, 2390.0, tp1=2402.0, actual_price=2400.0,
        source_name="GOLD DIGGERS INSTITUTIONAL", ignore_ime_bypass=True,
    )
    assert bypassed is None
    assert enforced is not None
    assert "R:R filter" in enforced


def test_the_static_bypass_sources_keep_their_exemption(fresh_db):
    """Gold Diggers VIP bypasses because it supplies its own TP/SL levels --
    a reason that staleness does not touch. Only the IME arm is suspended."""
    _configure_ime_channel("Gold Diggers VIP")
    result = rg.check_pre_trade_filters(
        "BUY", 2399.0, 2401.0, 2390.0, tp1=2402.0, actual_price=2400.0,
        source_name="Gold Diggers VIP", ignore_ime_bypass=True,
    )
    assert result is None


def test_a_fresh_signal_keeps_the_ime_bypass(fresh_db):
    """The control for the test above: without the flag, nothing changes."""
    _configure_ime_channel()
    result = rg.check_pre_trade_filters(
        "BUY", 2399.0, 2401.0, 2390.0, tp1=2402.0, actual_price=2400.0,
        source_name="GOLD DIGGERS INSTITUTIONAL", ignore_ime_bypass=False,
    )
    assert result is None


# ── The watcher, end to end ──────────────────────────────────────────────────

def test_a_stale_signal_with_bad_rr_is_not_activated(fresh_db):
    """THE test. Ten minutes paused, price sitting in the zone on resume, and
    a setup that would be declined on R:R if it had arrived just now. It must
    not open."""
    _configure_ime_channel()
    _insert_signal()
    gr.observe_watcher_run(time.time() - 600, [])

    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock()) as ot:
        asyncio.run(psa.try_activate_pending_signals(
            _TICK, _IME_RS, _FakeBridge(), {}, []))

    assert not ot.called
    with db.db() as conn:
        status = conn.execute(
            "SELECT status FROM vantage_signals WHERE signal_id='sig-1'"
        ).fetchone()[0]
    assert status == "pending"


def test_the_same_signal_activates_when_there_was_no_gap(fresh_db):
    """The control. Identical signal, identical price, watcher never away --
    the IME bypass still applies and the trade is taken. Without this the
    test above would pass just as well if the gate blocked everything."""
    _configure_ime_channel()
    _insert_signal()
    gr.observe_watcher_run(time.time(), [])

    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={
                               "entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(
            _TICK, _IME_RS, _FakeBridge(), {}, []))

    assert ot.called


def test_a_stale_signal_that_still_scores_well_is_activated(fresh_db):
    """Re-validation is not a blanket refusal. A signal whose reward:risk
    still stands up against the current price is taken, pause or no pause --
    otherwise this is just 'expire everything', which is not what was
    asked for."""
    _configure_ime_channel()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, "
            "entry_low, entry_high, stop_loss, tp1, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("sig-good", "GOLD DIGGERS INSTITUTIONAL", "BUY", 2399.0, 2401.0,
             2390.0, 2425.0, "pending", time.time()),
        )
    gr.observe_watcher_run(time.time() - 600, [])

    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={
                               "entry_price": 2400.5, "trade_id": "t"})) as ot:
        asyncio.run(psa.try_activate_pending_signals(
            _TICK, _IME_RS, _FakeBridge(), {}, []))

    assert ot.called


def test_an_activated_signal_stops_being_marked(fresh_db):
    """The mark is per signal and is dropped once that signal is resolved,
    or the set grows for the life of the process."""
    _configure_ime_channel()
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, "
            "entry_low, entry_high, stop_loss, tp1, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            ("sig-good", "GOLD DIGGERS INSTITUTIONAL", "BUY", 2399.0, 2401.0,
             2390.0, 2425.0, "pending", time.time()),
        )
    gr.observe_watcher_run(time.time() - 600, [])

    with mock.patch.object(psa, "get_open_trades", return_value=[]), \
         mock.patch.object(psa, "open_trade_from_signal",
                           new=mock.AsyncMock(return_value={
                               "entry_price": 2400.5, "trade_id": "t"})):
        asyncio.run(psa.try_activate_pending_signals(
            _TICK, _IME_RS, _FakeBridge(), {}, []))

    assert gr.needs_revalidation("sig-good") is False
