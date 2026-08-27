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


def test_the_ime_button_toggles_against_the_current_setting(fresh_db):
    """Was docs/todo/bugs/012: the panel read rs["ime_enabled"], which is not a
    column, so `on = not bool(None)` was always True and Immediate Market Entry
    could be switched on from Telegram but never off. The key is
    immediate_market_entry, which is what every other call site uses.

    Both directions asserted, because only one of them was ever broken.
    """
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


def _button_labels(screen) -> str:
    """The state a System button shows lives in its label, not the screen text."""
    return " | ".join(b.get("text", "") for row in (screen.keyboard or []) for b in row)


def test_the_system_menu_reports_the_real_ime_state(fresh_db):
    """The other half of the same bug: the status line read the same missing
    key, so it said OFF no matter what the setting was."""
    db.update_risk_settings({"immediate_market_entry": 1, "dpm_enabled": 0})
    assert "IME: ON" in _button_labels(panel.system_screen())

    db.update_risk_settings({"immediate_market_entry": 0})
    assert "IME: OFF" in _button_labels(panel.system_screen())


def test_the_system_menu_reports_the_real_dpm_state(fresh_db):
    """The neighbouring toggle, which was always correct -- asserted so the two
    stay readable side by side."""
    db.update_risk_settings({"dpm_enabled": 1})
    assert "DPM: ON" in _button_labels(panel.system_screen())

    db.update_risk_settings({"dpm_enabled": 0})
    assert "DPM: OFF" in _button_labels(panel.system_screen())


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


# ── Registration approval ─────────────────────────────────────────────────────
#
# The Telegram approval path for a remote client's licence request. Entirely
# uncovered before this. It reaches into remote.server's module state, so the
# tests replace that state rather than a fake object -- and every handler fires
# asyncio.create_task, so they have to run inside a loop.


@pytest.fixture
def remote(monkeypatch):
    """remote.server's registration state, replaced wholesale."""
    from backend.src.services.cluster.remote import server as rs

    approved = []

    def approve_registration(token, display_name, sub_type):
        approved.append((token, display_name, sub_type))
        return True

    async def _noop_push():
        return None

    monkeypatch.setattr(rs, "_pending", {}, raising=False)
    monkeypatch.setattr(rs, "_allowed_tokens", {}, raising=False)
    monkeypatch.setattr(rs, "_kg_insert_fn", None, raising=False)
    monkeypatch.setattr(rs, "_kg_get_all_fn", None, raising=False)
    monkeypatch.setattr(rs, "approve_registration", approve_registration, raising=False)
    monkeypatch.setattr(rs, "_save_pending", lambda: None, raising=False)
    monkeypatch.setattr(rs, "_push_pending_to_all_admins", _noop_push, raising=False)
    monkeypatch.setattr(rs, "_push_clients_to_all_admins", _noop_push, raising=False)
    monkeypatch.setattr(rs, "_push_licences_to_all_admins", _noop_push, raising=False)
    rs.approved = approved
    return rs


def _in_loop(fn, *args):
    """These handlers are sync but call asyncio.create_task, so they need a
    running loop underneath them."""
    async def _run():
        out = fn(*args)
        await asyncio.sleep(0)      # let the fire-and-forget pushes run
        return out
    return asyncio.run(_run())


TOKEN = "abcd1234" + "0" * 56          # a real token is 64 hex chars


def test_approving_a_request_that_is_gone_is_a_toast(fresh_db, remote):
    """The keyboard may be stale -- the request could have been handled from
    the admin console since it was drawn."""
    screen = _in_loop(panel._approve_registration, "deadbeef", "1y")

    assert screen.mode == "noop"
    assert "no longer pending" in (screen.toast or "")
    assert remote.approved == []


def test_a_request_is_addressed_by_the_first_eight_characters(fresh_db, remote):
    """callback_data cannot carry a 64-hex-char token -- Telegram's cap is 64
    BYTES for the whole payload -- so the short form has to resolve back."""
    remote._pending[TOKEN] = {"nickname": "Simon's VPS"}

    screen = _in_loop(panel._approve_registration, "abcd1234", "1y")

    assert remote.approved == [(TOKEN, "Simon's VPS", "1 Year")]
    assert screen.mode == "edit"
    assert "Simon's VPS" in screen.text


@pytest.mark.parametrize("code,label", [
    ("6m", "6 Months"), ("1y", "1 Year"), ("2y", "2 Years"),
    ("3y", "3 Years"), ("perp", "Perpetual"),
])
def test_each_duration_button_approves_for_that_term(fresh_db, remote, code, label):
    """Approving for the wrong term is a licence that expires at the wrong
    time, which nobody notices until it does."""
    remote._pending[TOKEN] = {"hostname": "vps-1"}

    _in_loop(panel._approve_registration, "abcd1234", code)

    assert remote.approved[0][2] == label


def test_an_unknown_duration_falls_back_to_perpetual(fresh_db, remote):
    remote._pending[TOKEN] = {"hostname": "vps-1"}
    _in_loop(panel._approve_registration, "abcd1234", "nonsense")
    assert remote.approved[0][2] == "Perpetual"


def test_the_display_name_prefers_nickname_then_hostname_then_the_token(fresh_db, remote):
    remote._pending[TOKEN] = {"nickname": "Nick", "hostname": "Host"}
    assert remote.approved == [] or True
    _in_loop(panel._approve_registration, "abcd1234", "1y")
    assert remote.approved[-1][1] == "Nick"

    remote._pending[TOKEN] = {"hostname": "Host"}
    _in_loop(panel._approve_registration, "abcd1234", "1y")
    assert remote.approved[-1][1] == "Host"

    remote._pending[TOKEN] = {}
    _in_loop(panel._approve_registration, "abcd1234", "1y")
    assert remote.approved[-1][1] == TOKEN[:8]


def test_a_failed_approval_says_so(fresh_db, remote, monkeypatch):
    from backend.src.services.cluster.remote import server as rs
    monkeypatch.setattr(rs, "approve_registration", lambda *a: False, raising=False)
    remote._pending[TOKEN] = {"hostname": "vps-1"}

    screen = _in_loop(panel._approve_registration, "abcd1234", "1y")

    assert screen.mode == "noop"
    assert "Approval failed" in (screen.toast or "")


def test_an_approval_with_no_licence_key_warns_on_screen(fresh_db, remote):
    """Silent success here means a client that thinks it is licensed and is
    not. The warning is the only sign the signing key is unregistered."""
    remote._pending[TOKEN] = {"hostname": "vps-1"}
    # _allowed_tokens left empty -> no licence_key was generated

    screen = _in_loop(panel._approve_registration, "abcd1234", "1y")

    assert "Licence key generation failed" in screen.text


def test_a_successful_approval_does_not_warn(fresh_db, remote):
    remote._pending[TOKEN] = {"hostname": "vps-1"}
    remote._allowed_tokens[TOKEN] = {"licence_key": "KEY-123", "machine_id": "M1"}

    screen = _in_loop(panel._approve_registration, "abcd1234", "1y")

    assert "Licence key generation failed" not in screen.text
    assert "✅ Approved" in screen.text


def test_rejecting_removes_the_request(fresh_db, remote):
    remote._pending[TOKEN] = {"nickname": "Spam VPS"}

    screen = _in_loop(panel._reject_registration, "abcd1234")

    assert TOKEN not in remote._pending, "a rejected request must not linger"
    assert screen.mode == "edit"
    assert "Spam VPS" in screen.text


def test_rejecting_something_already_gone_is_a_toast(fresh_db, remote):
    screen = _in_loop(panel._reject_registration, "deadbeef")
    assert screen.mode == "noop"
    assert "no longer pending" in (screen.toast or "")


# ── Mirroring the approval into the admin console ─────────────────────────────

def test_nothing_is_recorded_when_this_instance_has_no_licence_db(fresh_db, remote):
    """Only the instance running the admin console has the KeyGen callbacks
    registered; everywhere else this must no-op rather than raise."""
    remote._allowed_tokens[TOKEN] = {"licence_key": "K", "machine_id": "M"}
    _in_loop(panel._record_licence_issued, TOKEN)      # _kg_insert_fn is None


def test_an_approval_without_a_key_or_machine_id_is_not_recorded(fresh_db, remote, monkeypatch):
    from backend.src.services.cluster.remote import server as rs
    inserted = []
    monkeypatch.setattr(rs, "_kg_insert_fn", inserted.append, raising=False)
    remote._allowed_tokens[TOKEN] = {"licence_key": "", "machine_id": "M"}

    _in_loop(panel._record_licence_issued, TOKEN)

    assert inserted == [], "a half-formed licence must not reach the console"


def test_the_licence_row_carries_the_approval_details(fresh_db, remote, monkeypatch):
    from backend.src.services.cluster.remote import server as rs
    inserted = []
    monkeypatch.setattr(rs, "_kg_insert_fn", inserted.append, raising=False)
    remote._allowed_tokens[TOKEN] = {
        "licence_key": "KEY-123", "machine_id": "M1", "email": "s@example.com",
        "hostname": "vps-1", "platform": "darwin", "expiry_date": "2027-01-01",
        "subscription_type": "1 Year",
    }

    _in_loop(panel._record_licence_issued, TOKEN)

    assert len(inserted) == 1
    row = inserted[0]
    assert row["licence_key"] == "KEY-123"
    assert row["registration_id"] == "M1"
    assert row["macos_version"] == "macOS", "darwin should read as macOS"
    assert row["notes"] == "Auto-issued via Telegram approval"


def test_a_windows_client_is_labelled_windows(fresh_db, remote, monkeypatch):
    from backend.src.services.cluster.remote import server as rs
    inserted = []
    monkeypatch.setattr(rs, "_kg_insert_fn", inserted.append, raising=False)
    remote._allowed_tokens[TOKEN] = {
        "licence_key": "K", "machine_id": "M", "platform": "win32"}

    _in_loop(panel._record_licence_issued, TOKEN)
    assert inserted[0]["macos_version"] == "Windows"


def test_an_already_issued_licence_is_not_recorded_twice(fresh_db, remote, monkeypatch):
    """A second approval of the same key would show up twice in the Licence
    Manager."""
    from backend.src.services.cluster.remote import server as rs
    inserted = []
    monkeypatch.setattr(rs, "_kg_insert_fn", inserted.append, raising=False)
    monkeypatch.setattr(rs, "_kg_get_all_fn",
                        lambda: [{"licence_key": "KEY-123"}], raising=False)
    remote._allowed_tokens[TOKEN] = {"licence_key": "KEY-123", "machine_id": "M1"}

    _in_loop(panel._record_licence_issued, TOKEN)

    assert inserted == []

