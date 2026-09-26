"""The Mac's MT5 accounts, and its demo/live choice, reach the VPS.

Owner, 2026-09-26: "when using a vps it should send the credentials from the
local machine to it so it all stays in sync ... if i decide to switch from
demo/live ... those credentials should be on the vps and it should switch."
And, asked what a mismatch found on reconnect should do: switch the VPS to
match the Mac.

Found that day: the VPS's bridge was set up for demo account *480 while its
MetaTrader, and the Mac, were on *592; nothing had ever carried the Mac's
accounts across, because the settings sync deliberately never carries
credentials. This is its own message for that reason.

What travels: login, password and server for each account the Mac has
complete, and which one the Mac is on. Never the terminal path -- the Mac's
is inside a CrossOver bottle and means nothing on Windows.

Nothing here reaches MetaTrader, a broker or a socket: the credential store,
the environment switch and the bridge are recorders.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import _mt5_accounts_sync as acc
from backend.src.services.cluster.sync import protocol as P

MAC_CREDS = {
    "login": 26004592, "password_enc": "demo-pw", "server": "VantageMarkets-Demo",
    "terminal_path": "/Users/simon/Library/.../terminal64.exe",
    "live_login": 900123, "live_password_enc": "live-pw", "live_server": "VantageMarkets-Live",
    "live_terminal_path": None,
}


class TestWhatTheMacSends:
    def test_both_complete_accounts_and_the_current_environment(self, monkeypatch):
        monkeypatch.setattr(acc._creds, "get_mt5_credentials", lambda: dict(MAC_CREDS))
        monkeypatch.setattr(acc._env, "current", lambda: "demo")

        snap = acc.accounts_snapshot()

        assert snap["environment"] == "demo"
        assert snap["accounts"]["demo"] == {
            "login": "26004592", "password": "demo-pw", "server": "VantageMarkets-Demo"}
        assert snap["accounts"]["live"]["login"] == "900123"

    def test_never_the_terminal_path(self, monkeypatch):
        monkeypatch.setattr(acc._creds, "get_mt5_credentials", lambda: dict(MAC_CREDS))
        monkeypatch.setattr(acc._env, "current", lambda: "demo")

        assert "terminal" not in json.dumps(acc.accounts_snapshot())

    def test_an_incomplete_account_is_not_sent(self, monkeypatch):
        """Sending a login with no password would blank the VPS's working one."""
        creds = dict(MAC_CREDS, live_password_enc="")
        monkeypatch.setattr(acc._creds, "get_mt5_credentials", lambda: creds)
        monkeypatch.setattr(acc._env, "current", lambda: "demo")

        assert "live" not in acc.accounts_snapshot()["accounts"]


class _Vps:
    """The VPS's side: its store, its environment and its bridge, recorded."""

    def __init__(self, env="demo", switch_error=None):
        self.env = env
        self.switch_error = switch_error
        self.saved: list[dict] = []
        self.switched: list[str] = []
        self.bridge_files: list[str] = []
        self.aligned: list = []

    def kw(self):
        async def _align(bridge):
            self.aligned.append(bridge)
            return {"status": "repointed"}

        def _switch(target):
            if self.switch_error:
                raise ValueError(self.switch_error)
            self.switched.append(target)
            self.env = target
            return {"environment": target}

        return dict(save=self.saved.append, current=lambda: self.env, switch=_switch,
                    write_bridge_file=lambda e: self.bridge_files.append(e) or True,
                    align=_align)


def _msg(environment="demo", **accounts):
    accounts = accounts or {"demo": {"login": "26004592", "password": "demo-pw",
                                     "server": "VantageMarkets-Demo"}}
    return {"accounts": accounts, "environment": environment}


def _apply(vps, msg, bridge="BRIDGE"):
    return asyncio.run(acc.apply_accounts(msg, bridge, **vps.kw()))


class TestWhatTheVpsDoes:
    def test_it_stores_the_macs_accounts(self):
        vps = _Vps()

        _apply(vps, _msg())

        assert vps.saved == [{"login": 26004592, "password_enc": "demo-pw",
                              "server": "VantageMarkets-Demo"}]

    def test_on_the_same_account_the_bridge_is_logged_into_it(self):
        """The 2026-09-26 VPS: bridge set for *480, the Mac on *592. The new
        credentials must reach the running bridge, not wait for a restart."""
        vps = _Vps(env="demo")

        result = _apply(vps, _msg("demo"))

        assert result["switched"] is False
        assert vps.bridge_files == ["demo"] and vps.aligned == ["BRIDGE"]
        assert vps.switched == []

    def test_a_different_environment_switches_the_vps(self):
        vps = _Vps(env="demo")

        result = _apply(vps, _msg("live",
                                  live={"login": "900123", "password": "live-pw",
                                        "server": "VantageMarkets-Live"}))

        assert vps.switched == ["live"]
        assert result == {"switched": True, "environment": "live", "error": ""}

    def test_credentials_are_stored_before_the_switch_reads_them(self):
        """environment.switch refuses an account with no saved credentials;
        the ones that just arrived must already be there."""
        vps = _Vps(env="demo")
        order = []
        kw = vps.kw()
        save, switch = kw["save"], kw["switch"]
        kw["save"] = lambda u: order.append("save") or save(u)
        kw["switch"] = lambda t: order.append("switch") or switch(t)

        asyncio.run(acc.apply_accounts(
            _msg("live", live={"login": "1", "password": "p", "server": "s"}), None, **kw))

        assert order == ["save", "switch"]

    def test_a_refused_switch_changes_nothing_and_says_why(self):
        vps = _Vps(env="demo", switch_error="No Live MT5 credentials are saved.")

        result = _apply(vps, {"accounts": {}, "environment": "live"})

        assert result["switched"] is False
        assert result["environment"] == "demo"
        assert "No Live MT5 credentials" in result["error"]

    def test_an_account_the_mac_does_not_send_is_left_alone(self):
        vps = _Vps()

        _apply(vps, _msg("demo"))

        assert all("live_password_enc" not in s for s in vps.saved)

    def test_an_unknown_environment_is_ignored(self):
        vps = _Vps(env="demo")

        _apply(vps, _msg("paper"))

        assert vps.switched == []


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class TestTheClientPushes:
    def _client(self, connected=True):
        c = acc.ClientMt5AccountsMixin()
        c._ws = _Ws()
        c.conn_state = P.CONN_CONNECTED if connected else P.CONN_DISCONNECTED
        return c

    def test_it_sends_the_snapshot_when_connected(self, monkeypatch):
        monkeypatch.setattr(acc, "accounts_snapshot",
                            lambda: {"accounts": {}, "environment": "demo"})
        c = self._client()

        asyncio.run(c.push_mt5_accounts())

        assert c._ws.sent[0]["type"] == P.MSG_MT5_ACCOUNTS
        assert c._ws.sent[0]["environment"] == "demo"

    def test_it_sends_nothing_when_not_connected(self, monkeypatch):
        monkeypatch.setattr(acc, "accounts_snapshot",
                            lambda: {"accounts": {}, "environment": "demo"})
        c = self._client(connected=False)

        asyncio.run(c.push_mt5_accounts())

        assert c._ws.sent == []

    def test_a_refusal_from_the_vps_is_alerted(self, monkeypatch):
        alerts = []

        async def _alert(text):
            alerts.append(text)

        monkeypatch.setattr(acc, "_alert", _alert)
        c = self._client()

        asyncio.run(c._on_mt5_accounts_ack({"error": "No Live MT5 credentials are saved.",
                                            "environment": "demo", "switched": False}))

        assert alerts and "No Live MT5 credentials" in alerts[0]

