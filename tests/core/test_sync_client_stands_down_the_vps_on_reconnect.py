"""Whenever this machine is the active trader, a reconnecting VPS stands down.

Owner's decision, 2026-09-25: the Mac may take over without the VPS when the
VPS cannot be reached (handover.take_over_without_peer). The VPS never heard
about it, so if it comes back it may still believe it owns the account. The
first thing the Mac does on a new link, while it is LOCAL, is tell the VPS to
stand down. That is right in every case, not only after a forced take-over:
LOCAL means the VPS must not open trades.

On the VPS, a stand-down that arrives while it is ALREADY standing down must
not forget which engines it stopped the first time, or a later hand-back
restarts nothing.

Nothing here opens a socket or reaches a broker: the client's request and the
server's websocket are recorders, and the database is a dict.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import client as client_mod
from backend.src.services.cluster.sync import server as server_mod


@pytest.fixture
def client(monkeypatch):
    c = client_mod.SyncClient()
    c.asked: list = []
    c.alerts: list = []

    async def _stand_down(timeout=15.0):
        c.asked.append("stand_down")
        if getattr(c, "fail", None):
            raise c.fail
        return {"open_positions": [{"trade_id": 7}]}

    async def _alert(msg):
        c.alerts.append(msg)

    monkeypatch.setattr(c, "request_stand_down", _stand_down)
    monkeypatch.setattr(client_mod, "_alert_operator", _alert)
    return c


def _trader(monkeypatch, value):
    monkeypatch.setattr(client_mod.db_module, "get_active_trader", lambda: value)


def test_a_local_machine_stands_the_vps_down_on_connect(client, monkeypatch):
    _trader(monkeypatch, "local")

    asyncio.run(client.stand_down_peer_if_local())

    assert client.asked == ["stand_down"]


def test_a_remote_machine_leaves_the_vps_trading(client, monkeypatch):
    _trader(monkeypatch, "remote_vps")

    asyncio.run(client.stand_down_peer_if_local())

    assert client.asked == []


def test_a_vps_that_will_not_stand_down_is_alerted_not_raised(client, monkeypatch):
    """Two nodes may be trading one account. The operator must hear about it;
    the link itself should not die over it."""
    _trader(monkeypatch, "local")
    client.fail = TimeoutError("no ack")

    asyncio.run(client.stand_down_peer_if_local())

    assert client.alerts and "stand down" in client.alerts[0]


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def test_a_second_stand_down_keeps_the_engines_the_first_one_stopped(monkeypatch):
    db = {"trader": "local", "stood": ["breakout", "reversal_engine"]}
    monkeypatch.setattr(server_mod.db_module, "get_active_trader", lambda: db["trader"])
    monkeypatch.setattr(server_mod.db_module, "set_active_trader",
                        lambda v, *a, **k: db.__setitem__("trader", v))
    monkeypatch.setattr(server_mod.db_module, "get_stood_down_engines", lambda: list(db["stood"]))
    monkeypatch.setattr(server_mod.db_module, "set_stood_down_engines",
                        lambda v: db.__setitem__("stood", list(v)))
    srv = server_mod.SyncServer()   # no engines running: all already stopped
    ws = _Ws()

    asyncio.run(srv._handle_stand_down(ws))

    assert sorted(db["stood"]) == ["breakout", "reversal_engine"]
    assert ws.sent[0]["type"] == server_mod.MSG_STAND_DOWN_ACK
