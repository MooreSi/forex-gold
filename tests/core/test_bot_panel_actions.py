"""The Telegram panel's action handlers.

`core_bot_panel.py` is 1,709 lines at 67% coverage -- 331 statements never
executed -- and it is both the largest file in `services/positions` and one
blocked from being split for want of tests.

The handlers below were entirely uncovered. They are also the ones that do
things: pausing trading, switching demo/live, and closing positions. The module
is built so that this is testable without a network or a broker -- *"pure logic
-- it builds screens and mutates settings, but performs NO Telegram HTTP
itself"* -- and every handler takes its collaborator as `ctx`, so the fakes here
are SimpleNamespace objects and nothing can reach MT5 or Telegram.

Deliberately a separate file from test_bot_panel.py so it can take `fresh_db`
from tests/conftest.py. That file defines its own, and adding a 96th local copy
would push the fixture-dedup ratchet further over its baseline.
"""
from __future__ import annotations

import asyncio
import types
import uuid

import pytest

from backend.src.db import database as db
from backend.src.services.positions import core_bot_panel as panel


def _ctx(**over):
    """A stand-in for the engine. Every method records that it was called."""
    calls = []

    def _cmd(name, result=None):
        async def _fn(args=None):
            calls.append((name, args))
            return result if result is not None else f"{name} ok"
        return _fn

    ns = types.SimpleNamespace(
        calls=calls,
        _cmd_pause=_cmd("pause"), _cmd_resume=_cmd("resume"),
        _cmd_dpm_on=_cmd("dpm_on"), _cmd_dpm_off=_cmd("dpm_off"),
        _cmd_ime_on=_cmd("ime_on"), _cmd_ime_off=_cmd("ime_off"),
        _cmd_activate=_cmd("activate"), _cmd_report=_cmd("report"),
        _cmd_restart_bridge=_cmd("restart_bridge"), _cmd_restart_app=_cmd("restart_app"),
        _cmd_switch_demo=_cmd("switch_demo"), _cmd_switch_live=_cmd("switch_live"),
    )
    for k, v in over.items():
        setattr(ns, k, v)
    return ns


# ── _system_action ────────────────────────────────────────────────────────────
#
# The System menu. Every one of these was uncovered, including the two that
# switch the account environment.

@pytest.mark.parametrize("act,expected", [
    ("pause", "pause"), ("resume", "resume"),
    ("activate", "activate"), ("report", "report"),
    ("restartbridge", "restart_bridge"), ("restartapp", "restart_app"),
    ("demo", "switch_demo"), ("live", "switch_live"),
])
def test_each_system_button_calls_its_command(fresh_db, act, expected):
    ctx = _ctx()
    screen = asyncio.run(panel._system_action(act, ctx))

    assert [c[0] for c in ctx.calls] == [expected]
    assert screen.mode == "send"


def test_pause_asks_for_thirty_minutes(fresh_db):
    """The button has no duration picker, so the default is the behaviour."""
    ctx = _ctx()
    asyncio.run(panel._system_action("pause", ctx))
    assert ctx.calls == [("pause", ["30m"])]


def test_the_dpm_button_toggles_against_the_current_setting(fresh_db):
    """A toggle that always turned the feature ON would be a button that only
    works once."""
    db.update_risk_settings({"dpm_enabled": 0})
    ctx = _ctx()
    asyncio.run(panel._system_action("dpm", ctx))
    assert [c[0] for c in ctx.calls] == ["dpm_on"]

    db.update_risk_settings({"dpm_enabled": 1})
    ctx = _ctx()
    asyncio.run(panel._system_action("dpm", ctx))
    assert [c[0] for c in ctx.calls] == ["dpm_off"]


def test_the_ime_button_reads_a_key_that_does_not_exist(fresh_db):
    """BUG, pinned as it stands. See docs/todo/bugs/012.

    The panel reads `rs.get("ime_enabled")`. There is no such key: the column
    is `immediate_market_entry`, which is what every other call site uses
    (scan_messages.py:224 among them). `.get()` returns None, so the toggle
    computes `on = not False` every single time.

    The button therefore turns Immediate Market Entry ON and can never turn it
    OFF, and the System menu's status line always reads OFF regardless.

    This test documents the broken behaviour rather than the intended one,
    because fixing it changes when orders are placed at market and that needs
    the owner's sign-off. The test below it is the one that flips when it is
    fixed.
    """
    db.update_risk_settings({"immediate_market_entry": 1})   # IME genuinely ON
    ctx = _ctx()
    asyncio.run(panel._system_action("ime", ctx))

    assert [c[0] for c in ctx.calls] == ["ime_on"], (
        "if this now says ime_off, the key has been fixed -- see the xfail below"
    )


@pytest.mark.xfail(strict=True, reason=(
    "docs/todo/bugs/012: core_bot_panel reads rs['ime_enabled'], which does not "
    "exist; the column is immediate_market_entry. Fixing it changes when orders "
    "are placed at market, so it needs the owner's sign-off. When that lands, "
    "this xfail turns into an XPASS failure -- remove the marker and delete the "
    "broken-behaviour test above it."
))
def test_the_ime_button_should_toggle_against_the_real_setting(fresh_db):
    db.update_risk_settings({"immediate_market_entry": 0})
    ctx = _ctx()
    asyncio.run(panel._system_action("ime", ctx))
    assert [c[0] for c in ctx.calls] == ["ime_on"]

    db.update_risk_settings({"immediate_market_entry": 1})
    ctx = _ctx()
    asyncio.run(panel._system_action("ime", ctx))
    assert [c[0] for c in ctx.calls] == ["ime_off"], (
        "IME is on, so the button must offer to turn it off"
    )


def test_an_unknown_system_action_does_nothing_at_all(fresh_db):
    """Stale callback data from an old keyboard must be inert, not an error
    and certainly not a different action."""
    ctx = _ctx()
    screen = asyncio.run(panel._system_action("who_knows", ctx))

    assert screen.mode == "noop"
    assert ctx.calls == []


# ── Closing trades ────────────────────────────────────────────────────────────

def _open_trade(conn, *, ticket=111, direction="BUY", lots=0.1, trade_id=None):
    tid = trade_id or uuid.uuid4().hex[:16]
    sid = f"sig-{ticket}"
    conn.execute(
        "INSERT INTO vantage_signals "
        "(signal_id, direction, entry_low, entry_high, stop_loss, created_at) "
        "VALUES (?,?,?,?,?,0)", (sid, direction, 2399.0, 2401.0, 2390.0))
    conn.execute(
        "INSERT INTO vantage_simulated_trades "
        "(trade_id, signal_id, mt5_ticket, direction, entry_low, entry_high, entry_price, "
        " lot_size, remaining_lots, stop_loss, status, open_time, net_pnl) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,'open',0,0)",
        (tid, sid, ticket, direction, 2399.0, 2401.0, 2400.0, lots, lots, 2390.0))
    return tid


def test_closing_a_trade_that_is_no_longer_open_is_a_toast_not_a_close(fresh_db):
    """The keyboard may be stale -- the trade could have hit TP since it was
    drawn. Nothing should be sent to the close path."""
    closed = []

    async def close_trade(tid, reason):
        closed.append(tid)
        return {}

    screen = asyncio.run(panel._close_one("deadbeef", _ctx(close_trade=close_trade)))

    assert screen.mode == "noop"
    assert "no longer open" in (screen.toast or "")
    assert closed == [], "a stale button must not close anything"


def test_closing_one_trade_reports_the_realised_pnl(fresh_db):
    with fresh_db.db() as conn:
        tid = _open_trade(conn, direction="BUY", lots=0.1)

    async def close_trade(trade_id, reason):
        assert trade_id == tid
        assert reason == "manual_close"
        return {"net_pnl": 35.0, "close_price": 2435.0}

    screen = asyncio.run(panel._close_one(tid[:8], _ctx(close_trade=close_trade)))

    assert screen.mode == "send"
    assert "+$35.00" in screen.text
    assert "2435.00" in screen.text


def test_a_loss_is_shown_without_a_stray_plus_sign(fresh_db):
    with fresh_db.db() as conn:
        tid = _open_trade(conn)

    async def close_trade(trade_id, reason):
        return {"net_pnl": -12.5, "close_price": 2387.5}

    screen = asyncio.run(panel._close_one(tid[:8], _ctx(close_trade=close_trade)))
    assert "$-12.50" in screen.text and "+$-" not in screen.text


def test_a_close_that_fails_says_so_rather_than_claiming_success(fresh_db):
    with fresh_db.db() as conn:
        tid = _open_trade(conn)

    async def close_trade(trade_id, reason):
        raise RuntimeError("bridge refused")

    screen = asyncio.run(panel._close_one(tid[:8], _ctx(close_trade=close_trade)))

    assert screen.mode == "send"
    assert "Close failed" in screen.text and "bridge refused" in screen.text


def test_closing_many_totals_the_pnl(fresh_db):
    trades = [{"trade_id": "a" * 16, "direction": "BUY", "lot_size": 0.1, "mt5_ticket": 1},
              {"trade_id": "b" * 16, "direction": "SELL", "lot_size": 0.2, "mt5_ticket": 2}]

    async def close_trade(trade_id, reason):
        return {"net_pnl": 10.0 if trade_id.startswith("a") else 5.5,
                "close_price": 2400.0}

    text = asyncio.run(panel._close_many(trades, _ctx(close_trade=close_trade), "GD VIP"))

    assert "Closing 2 trade(s)" in text and "GD VIP" in text
    assert "Total P&L: +$15.50" in text


def test_one_failure_does_not_stop_the_rest_or_corrupt_the_total(fresh_db):
    """A basket close that gave up halfway would leave the user with positions
    they believe are shut."""
    trades = [{"trade_id": "a" * 16, "direction": "BUY", "lot_size": 0.1, "mt5_ticket": 1},
              {"trade_id": "b" * 16, "direction": "SELL", "lot_size": 0.2, "mt5_ticket": 2}]

    async def close_trade(trade_id, reason):
        if trade_id.startswith("a"):
            raise RuntimeError("nope")
        return {"net_pnl": 5.5, "close_price": 2400.0}

    text = asyncio.run(panel._close_many(trades, _ctx(close_trade=close_trade), "GD VIP"))

    assert "Failed 1: nope" in text, "the failure must be reported, by ticket"
    assert "Total P&L: +$5.50" in text, "and must not be counted in the total"
