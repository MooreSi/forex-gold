"""Telegram signals, and the Signal Generator engines, run on the node that trades.

Owner, 2026-09-29: "when the VPS is the active node it should receive and
parse the telegram signals, this is the whole purpose of a vps as it is closer
to the exchange ... the mac can change the parsing, risk settings etc." And,
for the node that is not trading: "keep parsing for display only, no trades,
no alerts". Spec: docs/todo/010-telegram-parses-on-the-active-node.md.

Found that day. With `centralized_signal_gen_enabled` on, the VPS skipped
Telegram entirely and every signal took the Mac -> VPS hop. With it off, both
nodes parsed and the Mac's attempts were refused one by one ("Trading stood
down"), and the three engines generated NOWHERE: each one's cycle returned at
`is_remote_node()` on the VPS, while the Mac's orders were refused.

Nothing here reaches MetaTrader, a broker, Telegram or a socket. Every
collaborator that could act is a recorder that fails the test if called.
"""
from __future__ import annotations

import asyncio
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.cluster import node_roles
from backend.src.services.cluster.sync.protocol import TRADER_LOCAL, TRADER_REMOTE_VPS
from backend.src.services.signals import scan_messages as sm
from backend.src.services.telegram import alerts as telegram_alerts


# ── Which node is which ──────────────────────────────────────────────────────

@pytest.fixture
def node(monkeypatch):
    """Baseline: a standalone install. Tests turn it into a Mac or a VPS."""
    from backend.src.services.cluster.sync import client as sync_client
    from backend.src.services.cluster.sync import server as sync_server

    state = {"role": "standalone", "active_trader": TRADER_LOCAL, "centralized": 0}

    class _Server:
        def is_standing_down(self):
            return state["active_trader"] == TRADER_LOCAL

    monkeypatch.setattr(sync_server, "get_instance",
                        lambda: _Server() if state["role"] == "vps" else None)
    monkeypatch.setattr(sync_client.SyncClient, "load_config", staticmethod(
        lambda: ("203.0.113.10" if state["role"] == "mac" else "", 8765, "tok")))
    monkeypatch.setattr(db, "get_active_trader", lambda: state["active_trader"])
    real_get = db.get_app_config
    monkeypatch.setattr(db, "get_app_config", lambda k, *a, **kw: (
        ("1" if state["role"] == "vps" else "0") if k == "sync_server_enabled"
        else real_get(k, *a, **kw)))
    real_rs = db.get_risk_settings
    monkeypatch.setattr(db, "get_risk_settings", lambda: {
        **real_rs(), "centralized_signal_gen_enabled": state["centralized"]})
    return state


class TestWhereTheEnginesGenerate:
    """`node_roles.engines_generate_here()`: the gate all three engines ask."""

    @pytest.mark.parametrize("centralized", [0, 1])
    def test_a_standalone_install_always_generates(self, fresh_db, node, centralized):
        node["centralized"] = centralized
        assert node_roles.engines_generate_here() is True

    def test_THE_ACTIVE_VPS_GENERATES_WHEN_NOT_CENTRALIZED(self, fresh_db, node):
        """The owner's setup on 2026-09-29, and the bug: False before this."""
        node.update(role="vps", active_trader=TRADER_REMOTE_VPS, centralized=0)
        assert node_roles.engines_generate_here() is True

    def test_the_active_vps_does_not_when_the_mac_is_centralized(self, fresh_db, node):
        node.update(role="vps", active_trader=TRADER_REMOTE_VPS, centralized=1)
        assert node_roles.engines_generate_here() is False

    @pytest.mark.parametrize("centralized", [0, 1])
    def test_a_standby_vps_never_generates(self, fresh_db, node, centralized):
        node.update(role="vps", active_trader=TRADER_LOCAL, centralized=centralized)
        assert node_roles.engines_generate_here() is False

    def test_A_STANDBY_MAC_DOES_NOT_GENERATE_WHEN_NOT_CENTRALIZED(self, fresh_db, node):
        """Every order it made was refused by open_trade's stand-down gate."""
        node.update(role="mac", active_trader=TRADER_REMOTE_VPS, centralized=0)
        assert node_roles.engines_generate_here() is False

    def test_a_standby_mac_generates_and_forwards_when_centralized(self, fresh_db, node):
        """Engine forwarding (MSG_SIGNAL_ORDER) is unchanged."""
        node.update(role="mac", active_trader=TRADER_REMOTE_VPS, centralized=1)
        assert node_roles.engines_generate_here() is True

    @pytest.mark.parametrize("centralized", [0, 1])
    def test_a_mac_in_local_mode_generates(self, fresh_db, node, centralized):
        node.update(role="mac", active_trader=TRADER_LOCAL, centralized=centralized)
        assert node_roles.engines_generate_here() is True

    def test_exactly_one_node_generates_in_every_mode(self, fresh_db, node):
        """The pairing invariant. Two generating nodes is two sets of orders
        for one setup; none is a Signal Generator that has silently stopped."""
        for trader in (TRADER_LOCAL, TRADER_REMOTE_VPS):
            for centralized in (0, 1):
                node.update(active_trader=trader, centralized=centralized)
                node["role"] = "mac"
                mac = node_roles.engines_generate_here()
                node["role"] = "vps"
                vps = node_roles.engines_generate_here()
                assert [mac, vps].count(True) == 1, (trader, centralized, mac, vps)

    def test_an_error_generates_nothing(self, fresh_db, node, monkeypatch):
        """Fails closed, as `is_remote_node()` did: a node that cannot tell
        whether it is the trader must not risk being the second one."""
        monkeypatch.setattr(db, "get_active_trader",
                            mock.Mock(side_effect=RuntimeError("db gone")))
        node["role"] = "vps"
        assert node_roles.engines_generate_here() is False


class TestEachEngineAsksTheGate:
    """Before this change each cycle returned at `is_remote_node()` on the
    VPS unconditionally. They must ask `engines_generate_here()` instead."""

    def test_trend_pa(self, monkeypatch):
        from backend.src.services.trend_pa import service
        monkeypatch.setattr(db, "is_remote_node", lambda: True)
        monkeypatch.setattr(node_roles, "engines_generate_here", lambda: True)
        assert asyncio.run(service._generates_here()) is True
        monkeypatch.setattr(node_roles, "engines_generate_here", lambda: False)
        assert asyncio.run(service._generates_here()) is False

    def test_breakout(self, monkeypatch):
        from backend.src.services.breakout_signal import breakout_signal_service as bo
        eng = bo.BreakoutEngine.__new__(bo.BreakoutEngine)
        eng.status_detail = ""
        monkeypatch.setattr(db, "is_remote_node", lambda: True)
        monkeypatch.setattr(node_roles, "engines_generate_here", lambda: False)
        asyncio.run(eng._run_cycle())
        assert eng.status_detail == bo.NOT_GENERATING_HERE

    def test_reversal(self, monkeypatch):
        from backend.src.services.reversal_engine import reversal_engine_service as re_svc
        eng = re_svc.ReversalEngine.__new__(re_svc.ReversalEngine)
        eng._status_msg = ""
        monkeypatch.setattr(db, "is_remote_node", lambda: True)
        monkeypatch.setattr(node_roles, "engines_generate_here", lambda: False)
        asyncio.run(eng._run_cycle())
        assert eng._status_msg == re_svc.NOT_GENERATING_HERE


# ── The standby node: parse for display, nothing else ────────────────────────

_GD2_FULL = "XAU USD BUY NOW\n\n4534 - 4529\n\nTP1 4537\nTP2 4539\nTP3 4541\n\nSL 4527"
_IME = "XAU USD SELL NOW"
_CLOSE_ALL = "CLOSE ALL"
_CHATTER = "Good morning traders, big day ahead"


class _Reader:
    def __init__(self, messages):
        self._messages = messages

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return {"g1": 1}

    def get_group_name(self, group_id):
        return "Gold Diggers VIP"


CALLED: list = []


def _forbidden(name):
    """Records, then raises. Callers such as the CLOSE ALL trigger swallow
    exceptions, so a raise alone would prove nothing: every test checks
    CALLED as well."""
    async def _async(*a, **k):
        CALLED.append(name)
        raise AssertionError(f"the standby node called {name}")
    return _async


def _ctx(messages):
    CALLED.clear()

    def _sync_forbidden(*a, **k):
        CALLED.append("queue_unrecognised")
        raise AssertionError("the standby node queued an unrecognised message")
    return sm.ScanCtx(
        tg_reader=_Reader(messages), cfg={}, tg_off_warn_state={},
        close_trade=_forbidden("close_trade"),
        try_ai_signal_fallback=_forbidden("the AI fallback"),
        find_and_apply_instant_followup=_forbidden("the instant follow-up"),
        open_trade=_forbidden("open_trade"),
        queue_unrecognised=_sync_forbidden,
        get_trading_balance=_forbidden("get_trading_balance"),
    )


def _msg(tg_id, text):
    return {"id": tg_id, "group_id": "g1", "text": text, "timestamp": ""}


def _row(tg_id):
    with db.db() as conn:
        r = conn.execute("SELECT * FROM vantage_tg_signals WHERE tg_message_id=?",
                         (tg_id,)).fetchone()
    return dict(r) if r else None


@pytest.fixture
def standby_mac(fresh_db, node):
    db.update_risk_settings({"accept_tg_signals": 1, "auto_execute_signals": 1})
    db._rs_cache = None
    node.update(role="mac", active_trader=TRADER_REMOTE_VPS, centralized=0)
    return node


def _scan(ctx, standby=True):
    alerts = mock.AsyncMock()
    with mock.patch.object(telegram_alerts, "send_message", new=alerts):
        result = asyncio.run(sm.scan_messages(ctx))
    if standby:
        assert CALLED == []
    return result, alerts


class TestTheStandbyNodeOnlyRecords:
    def test_a_full_signal_is_recorded_as_standby(self, standby_mac):
        _result, alerts = _scan(_ctx([_msg("101", _GD2_FULL)]))

        row = _row("101")
        assert row is not None
        assert row["status"] == sm.STATUS_STANDBY
        assert (row["direction"], row["entry_low"], row["entry_high"], row["stop_loss"]) == \
            ("BUY", 4529.0, 4534.0, 4527.0)
        alerts.assert_not_called()

    def test_an_instant_entry_is_recorded_and_not_executed(self, standby_mac):
        _result, alerts = _scan(_ctx([_msg("102", _IME)]))

        row = _row("102")
        assert row is not None and row["status"] == sm.STATUS_STANDBY
        assert row["direction"] == "SELL"
        alerts.assert_not_called()

    def test_a_close_all_does_not_close_anything(self, standby_mac, monkeypatch):
        """close_trade is a recorder that fails the test if it is called. The
        channel has an open trade, so on an active node this message would
        close it (negative control below)."""
        from backend.src.services.telegram import keyword_triggers as kt
        monkeypatch.setattr(kt, "_find_channel_open_trade",
                            lambda ch: {"trade_id": "t-open-1", "strategy": ""})
        monkeypatch.setattr(kt, "_tg_cmd_blocked", lambda t: False)
        _scan(_ctx([_msg("103", _CLOSE_ALL)]))

    def test_negative_control_an_active_node_does_close_it(self, fresh_db, node, monkeypatch):
        from backend.src.services.telegram import keyword_triggers as kt
        db.update_risk_settings({"accept_tg_signals": 1})
        db._rs_cache = None
        monkeypatch.setattr(kt, "_find_channel_open_trade",
                            lambda ch: {"trade_id": "t-open-1", "strategy": ""})
        monkeypatch.setattr(kt, "_tg_cmd_blocked", lambda t: False)
        closed = []

        async def _close(trade_id, reason):
            closed.append(trade_id)
            return {}
        ctx = _ctx([_msg("108", _CLOSE_ALL)])
        ctx.close_trade = _close
        _scan(ctx, standby=False)
        assert closed == ["t-open-1"]

    def test_chatter_records_nothing_and_asks_no_ai(self, standby_mac):
        _scan(_ctx([_msg("104", _CHATTER)]))
        assert _row("104") is None

    def test_rescanning_does_not_record_twice(self, standby_mac):
        ctx = _ctx([_msg("105", _GD2_FULL)])
        _scan(ctx)
        _scan(ctx)
        with db.db() as conn:
            n = conn.execute("SELECT COUNT(*) FROM vantage_tg_signals "
                             "WHERE tg_message_id='105'").fetchone()[0]
        assert n == 1

    def test_the_signal_is_returned_for_the_dashboard(self, standby_mac):
        result, _ = _scan(_ctx([_msg("106", _GD2_FULL)]))
        assert [r["tg_message_id"] for r in result] == ["106"]
        assert result[0]["auto_executed"] is False

    def test_a_standby_vps_only_records_too(self, fresh_db, node):
        db.update_risk_settings({"accept_tg_signals": 1, "auto_execute_signals": 1})
        db._rs_cache = None
        node.update(role="vps", active_trader=TRADER_LOCAL)
        _scan(_ctx([_msg("107", _GD2_FULL)]))
        assert _row("107")["status"] == sm.STATUS_STANDBY


class TestTheActiveNodeParses:
    def test_THE_ACTIVE_VPS_PARSES_EVEN_WHEN_CENTRALIZED(self, fresh_db, node):
        """Before: `should_generate_signals_here()` was False here, so the
        scan returned [] and every Telegram signal waited on the Mac."""
        db.update_risk_settings({"accept_tg_signals": 1, "auto_execute_signals": 0})
        db._rs_cache = None
        node.update(role="vps", active_trader=TRADER_REMOTE_VPS, centralized=1)
        ctx = _ctx([_msg("201", _GD2_FULL)])
        ctx.is_trading_paused = lambda: False

        with mock.patch("backend.src.services.signals.scan_messages."
                        "_resolve_strategy_and_skip_reason_impl",
                        new=mock.AsyncMock(return_value={
                            "strategy": "s", "strategy_name": "S", "skip_reason": "",
                            "sess_ok": True, "per_signal_skip": False,
                            "per_signal_skip_reason": ""})):
            _scan(ctx, standby=False)

        row = _row("201")
        assert row is not None and row["status"] != sm.STATUS_STANDBY
