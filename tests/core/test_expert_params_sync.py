"""Expert Tunables reach the paired node, the way Strategy Parameters do.

`expert_params._forward_over_sync()` has called `cli.propose_expert_params`
since 2026-08-03, behind a `hasattr` guard -- and neither the sync client nor
the server ever had the method. Every Expert Tunables change on the Mac stayed
on the Mac while the VPS traded with its own values, and nothing said so.
Several of these gate order placement (the R:R floor among them).

Same shape as Strategy Parameters: the full snapshot is proposed, held until
the VPS's confirmed snapshot equals it, persisted across restarts, re-sent on
reconnect, and mirrored down when nothing is pending.

Nothing here opens a socket or reaches a broker.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_EXPERT_PARAMS_PROPOSE, MSG_EXPERT_PARAMS_STATE,
)
from backend.src.services.risk import expert_params as ep

pytestmark = [pytest.mark.usefixtures("fresh_db"), pytest.mark.asyncio]


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


def _a_key() -> str:
    return next(iter(ep.defaults()))


def _changed_snapshot() -> dict:
    key = _a_key()
    spec = ep._SPECS[key]
    snap = ep.defaults()
    snap[key] = spec.max
    return snap


@pytest.fixture
def mac(monkeypatch):
    client = sc.SyncClient()
    client.conn_state = CONN_CONNECTED
    client._ws = _Ws()
    monkeypatch.setattr(sc, "_instance", client)
    return client


class TestTheMacProposes:
    async def test_a_local_change_is_sent_to_the_vps(self, mac):
        await mac.propose_expert_params(_changed_snapshot())

        sent = [m for m in mac._ws.sent if m["type"] == MSG_EXPERT_PARAMS_PROPOSE]
        assert sent and sent[0]["expert_params"] == _changed_snapshot()

    async def test_it_is_held_and_survives_a_restart_until_confirmed(self, mac):
        await mac.propose_expert_params(_changed_snapshot())

        assert sc.SyncClient()._pending_expert_params == _changed_snapshot()

    async def test_the_vps_confirming_it_clears_the_queue(self, mac):
        await mac.propose_expert_params(_changed_snapshot())

        await mac._dispatch({"type": MSG_EXPERT_PARAMS_STATE,
                             "expert_params": _changed_snapshot()})

        assert mac._pending_expert_params is None
        assert sc.SyncClient()._pending_expert_params is None

    async def test_saving_a_tunable_reaches_the_client(self, mac, monkeypatch):
        """The whole route, from the service's own save."""
        scheduled: list = []
        monkeypatch.setattr(ep, "_schedule_coro", lambda coro: scheduled.append(coro))
        ep.set_params({_a_key(): ep._SPECS[_a_key()].max})
        for coro in scheduled:
            await coro

        assert mac._pending_expert_params is not None
        assert mac._pending_expert_params[_a_key()] == ep._SPECS[_a_key()].max


class TestTheMacMirrorsTheVps:
    async def test_with_nothing_pending_the_vps_values_are_applied_here(self, mac):
        await mac._dispatch({"type": MSG_EXPERT_PARAMS_STATE,
                             "expert_params": _changed_snapshot()})

        assert ep.all_values()[_a_key()] == ep._SPECS[_a_key()].max

    async def test_a_pending_local_edit_is_not_overwritten(self, mac):
        mine = _changed_snapshot()
        await mac.propose_expert_params(mine)

        await mac._dispatch({"type": MSG_EXPERT_PARAMS_STATE,
                             "expert_params": ep.defaults()})

        assert mac._pending_expert_params == mine


@pytest.fixture
def vps(monkeypatch):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._clients = set()
    srv.broadcasts = []

    async def _broadcast(msg):
        srv.broadcasts.append(msg)
    srv._broadcast = _broadcast
    return srv


class TestTheVpsApplies:
    async def test_it_applies_the_snapshot_and_confirms_it(self, vps):
        await vps._handle_expert_params_propose(
            _Ws(), {"type": MSG_EXPERT_PARAMS_PROPOSE,
                    "expert_params": _changed_snapshot()})

        assert ep.all_values()[_a_key()] == ep._SPECS[_a_key()].max
        assert [m["type"] for m in vps.broadcasts] == [MSG_EXPERT_PARAMS_STATE]
        assert vps.broadcasts[0]["expert_params"] == ep.all_values()

    async def test_applying_it_does_not_echo_it_back(self, vps, monkeypatch):
        forwarded: list = []
        monkeypatch.setattr(ep, "_forward_over_sync", lambda: forwarded.append(1))

        await vps._handle_expert_params_propose(
            _Ws(), {"expert_params": _changed_snapshot()})

        assert forwarded == []

    async def test_the_welcome_carries_them(self, vps):
        assert vps._expert_params_snapshot() == ep.all_values()
