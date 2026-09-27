"""Upgrade the VPS from the Mac's Settings > Remote Node (owner, 2026-09-27),
and show whether the two nodes run the same commit.

The VPS updates exactly as its own Settings > Update button does:
`core_app_update.apply_update()` (fetch + force-checkout origin/main, pip,
pycache, EA deploy, restart). This adds a way to ASK, not a second updater.
It answers at once -- an update takes minutes and ends in a restart that
drops the link -- and reports a failure afterwards if one happens. One at a
time: a second request while one runs is refused, not queued.

The heartbeat carries the VPS's commit and git version, so the Mac can say
whether the two nodes are in sync.

Nothing here updates, restarts or reaches git: apply_update and the commit
readers are replaced with recorders.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.sync import _update_sync as us
from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.fixture
def updater(monkeypatch):
    state = {"calls": 0, "result": {"ok": True, "error": None}, "gate": None}

    async def _apply(restart=True):
        state["calls"] += 1
        state["restart"] = restart
        if state["gate"] is not None:
            await state["gate"].wait()
        return state["result"]

    monkeypatch.setattr(us.core_app_update, "apply_update", _apply)
    monkeypatch.setattr(us, "_updating", False)
    return state


def _vps():
    return ss.SyncServer.__new__(ss.SyncServer)


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


class TestTheVpsSide:
    async def test_it_answers_at_once_then_updates_the_way_settings_update_does(self, updater):
        ws = _Ws()
        await _vps()._dispatch(ws, {"type": P.MSG_UPDATE_NODE})
        await _settle()

        assert ws.sent[0]["type"] == P.MSG_UPDATE_NODE_ACK
        assert ws.sent[0]["ok"] is True
        assert updater["calls"] == 1
        assert updater["restart"] is True

    async def test_a_failed_update_is_reported_afterwards(self, updater):
        updater["result"] = {"ok": False, "error": "git fetch failed"}
        ws = _Ws()
        await _vps()._dispatch(ws, {"type": P.MSG_UPDATE_NODE})
        await _settle()

        assert ws.sent[1] == {"type": P.MSG_UPDATE_NODE_RESULT, "ok": False,
                              "note": "git fetch failed"}

    async def test_one_at_a_time(self, updater):
        updater["gate"] = asyncio.Event()
        ws = _Ws()
        await _vps()._dispatch(ws, {"type": P.MSG_UPDATE_NODE})
        await _settle()
        await _vps()._dispatch(ws, {"type": P.MSG_UPDATE_NODE})
        await _settle()

        assert updater["calls"] == 1
        assert ws.sent[1]["ok"] is False
        assert "already" in ws.sent[1]["note"]
        updater["gate"].set()
        await _settle()

    async def test_an_update_that_raises_is_still_reported(self, updater, monkeypatch):
        async def _boom(restart=True):
            raise RuntimeError("disk full")

        monkeypatch.setattr(us.core_app_update, "apply_update", _boom)
        ws = _Ws()
        await _vps()._dispatch(ws, {"type": P.MSG_UPDATE_NODE})
        await _settle()

        assert ws.sent[1]["ok"] is False
        assert "disk full" in ws.sent[1]["note"]
        assert us._updating is False


class _MacWs:
    def __init__(self, client, reply=None):
        self.client = client
        self.reply = reply
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.reply is not None and msg["type"] == P.MSG_UPDATE_NODE:
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.client._dispatch(self.reply)))


def _mac(reply=None, connected=True):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = P.CONN_CONNECTED if connected else P.CONN_DISCONNECTED
    cli._ws = _MacWs(cli, reply)
    return cli


class TestTheMacSide:
    async def test_it_asks_and_returns_the_vps_answer(self):
        cli = _mac(reply={"type": P.MSG_UPDATE_NODE_ACK, "ok": True, "note": "Updating"})
        got = await cli.request_peer_update(timeout=2)
        assert cli._ws.sent == [{"type": P.MSG_UPDATE_NODE}]
        assert got["ok"] is True

    async def test_not_connected_sends_nothing(self):
        cli = _mac(connected=False)
        with pytest.raises(ConnectionError):
            await cli.request_peer_update(timeout=2)
        assert cli._ws.sent == []

    async def test_a_vps_that_never_answers_times_out(self):
        cli = _mac(reply=None)
        with pytest.raises(asyncio.TimeoutError):
            await cli.request_peer_update(timeout=0.2)

    async def test_a_later_failure_is_kept_for_the_screen(self):
        cli = _mac()
        await cli._dispatch({"type": P.MSG_UPDATE_NODE_RESULT, "ok": False, "note": "git fetch failed"})
        assert cli.last_update_result == {"ok": False, "note": "git fetch failed"}


# ── Versions ────────────────────────────────────────────────────────────────

SHA_A = "a" * 40
SHA_B = "b" * 40


class TestVersions:
    async def test_the_same_commit_is_in_sync(self, monkeypatch):
        monkeypatch.setattr(us, "_local", lambda: {"commit": SHA_A, "git_version": "2.39.5"})
        got = us.version_report({"commit": SHA_A, "git_version": "2.45.1"})
        assert got["in_sync"] is True
        assert got["local"]["git_version"] == "2.39.5"
        assert got["remote"]["git_version"] == "2.45.1"

    async def test_a_different_commit_is_not(self, monkeypatch):
        monkeypatch.setattr(us, "_local", lambda: {"commit": SHA_A, "git_version": ""})
        assert us.version_report({"commit": SHA_B})["in_sync"] is False

    async def test_a_vps_that_sends_no_commit_is_unknown_not_out_of_sync(self, monkeypatch):
        """An older VPS's heartbeat has no commit field: that is not evidence
        of a difference."""
        monkeypatch.setattr(us, "_local", lambda: {"commit": SHA_A, "git_version": ""})
        got = us.version_report({"balance": 1.0})
        assert got["in_sync"] is None
        assert got["remote"] is None

    async def test_no_link_is_unknown(self, monkeypatch):
        monkeypatch.setattr(us, "_local", lambda: {"commit": SHA_A, "git_version": ""})
        assert us.version_report({})["in_sync"] is None

    async def test_the_heartbeat_carries_the_commit_and_git_version(self, monkeypatch):
        monkeypatch.setattr(us.core_app_update, "get_local_commit_sha", lambda short=True: SHA_A)
        monkeypatch.setattr(us.core_app_update, "get_git_version", lambda: "2.45.1")
        assert us.heartbeat_fields() == {"commit": SHA_A, "git_version": "2.45.1"}

    async def test_an_unreadable_commit_never_breaks_the_heartbeat(self, monkeypatch):
        def _boom(short=True):
            raise OSError("no git")

        monkeypatch.setattr(us.core_app_update, "get_local_commit_sha", _boom)
        assert us.heartbeat_fields() == {"commit": "", "git_version": ""}
