"""The merge over the sync link (docs/todo/026).

What each side must never do:

  * the VPS must never write a pushed row anywhere but `ml_imported_rows`,
    nor accept rows that name the VPS itself as their origin;
  * the VPS must never hold its receive loop while it fits models: the same
    loop carries the Mac's close and resume commands;
  * the Mac must never send back rows it received: a node exports only its
    own, or each merge would bounce the other's history back with a new
    origin and count it twice;
  * the peer report must stay read-only and checked: a dataset it does not
    know is refused.

Temp databases and recorders. Nothing here reaches a broker.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster import peer_reports as pr
from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss
from backend.src.services.ml_merge import repo as mrepo
from backend.src.services.ml_merge import service
from tests.ml_merge.conftest import OWN, PEER, add_re, re_row


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.fixture
def on_the_vps(monkeypatch):
    monkeypatch.setattr(service, "own_node_id", lambda: PEER)


def _server():
    return ss.SyncServer.__new__(ss.SyncServer)


@pytest.mark.asyncio
class TestTheVpsSide:

    async def test_pushed_rows_are_stored_as_imported_and_acknowledged(self, dbs, on_the_vps):
        ws = _Ws()
        rows = [{**re_row(f"RE-{i}", 1_000.0 * i), "id": i + 1} for i in range(3)]

        await _server()._dispatch(ws, {"type": P.MSG_ML_HISTORY_PUSH, "req_id": "p1",
                                       "kind": "re", "origin_node": OWN, "rows": rows})

        assert ws.sent == [{"type": P.MSG_ML_MERGE_ACK, "req_id": "p1", "inserted": 3}]
        assert mrepo.count("re") == 3
        from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
        assert re_repo.get_db().get("SELECT COUNT(*) FROM re_signals")[0] == 0

    async def test_rows_naming_the_vps_as_their_origin_are_refused(self, dbs, on_the_vps):
        ws = _Ws()

        await _server()._dispatch(ws, {"type": P.MSG_ML_HISTORY_PUSH, "req_id": "p2",
                                       "kind": "re", "origin_node": PEER,
                                       "rows": [{**re_row("RE-1", 1.0), "id": 1}]})

        assert "error" in ws.sent[0] and "inserted" not in ws.sent[0]
        assert mrepo.count("re") == 0

    async def test_the_check_answers_later_without_holding_the_loop(self, monkeypatch):
        release = asyncio.Event()

        async def _slow():
            await release.wait()
            return {"re": {"use": True}}

        monkeypatch.setattr(service, "apply_async", _slow)
        ws = _Ws()

        await asyncio.wait_for(_server()._dispatch(
            ws, {"type": P.MSG_ML_MERGE_EVALUATE, "req_id": "e1"}), timeout=1)
        assert ws.sent == []          # dispatch returned before the check finished

        release.set()
        for _ in range(20):
            await asyncio.sleep(0)
        assert ws.sent == [{"type": P.MSG_ML_MERGE_ACK, "req_id": "e1",
                            "result": {"re": {"use": True}}}]

    async def test_the_history_report_pages_own_rows_and_names_the_node(self, dbs, monkeypatch):
        monkeypatch.setattr("backend.src.db.database.get_or_create_node_id", lambda: PEER)
        for i in range(3):
            add_re(re_row(f"RE-{i}", float(i)))

        got = await pr.run_local("ml_history", {"kind": "re", "after": 0, "limit": 2})

        assert got["node_id"] == PEER
        assert [r["signal_ref"] for r in got["rows"]] == ["RE-0", "RE-1"]
        assert got["done"] is False

    async def test_the_history_report_refuses_a_dataset_it_does_not_know(self):
        with pytest.raises(ValueError):
            await pr.run_local("ml_history", {"kind": "vantage_signals"})


class _MacWs:
    def __init__(self, client):
        self.client = client
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        ack = {"type": P.MSG_ML_MERGE_ACK, "req_id": msg["req_id"], "inserted": 2}
        asyncio.get_running_loop().call_soon(
            lambda: asyncio.ensure_future(self.client._dispatch(ack)))


@pytest.mark.asyncio
class TestTheMacSide:

    async def test_a_push_goes_out_and_returns_the_ack(self):
        cli = sc.SyncClient.__new__(sc.SyncClient)
        cli.conn_state = P.CONN_CONNECTED
        cli._ws = _MacWs(cli)

        got = await cli.push_ml_rows("re", OWN, [{"id": 1}], timeout=2)

        assert cli._ws.sent[0]["type"] == P.MSG_ML_HISTORY_PUSH
        assert cli._ws.sent[0]["origin_node"] == OWN
        assert got["inserted"] == 2

    async def test_not_connected_sends_nothing(self):
        cli = sc.SyncClient.__new__(sc.SyncClient)
        cli.conn_state = P.CONN_DISCONNECTED
        cli._ws = _MacWs(cli)
        with pytest.raises(ConnectionError):
            await cli.push_ml_rows("re", OWN, [], timeout=1)
        assert cli._ws.sent == []


class _FakeVps:
    """The VPS as the Mac sees it: one page of Reversal rows to give, a
    recorder for what is pushed, and a canned benefit check."""

    def __init__(self, peer_rows, old=False):
        self.peer_rows, self.old = peer_rows, old
        self.pushed: list[tuple] = []

    async def request_peer_report(self, name, args, **kw):
        if self.old:
            return {"error": "unknown report 'ml_history'"}
        rows = self.peer_rows if args["kind"] == "re" else []
        return {"result": {"kind": args["kind"], "rows": rows, "next_after": 0,
                           "done": True, "node_id": PEER}}

    async def push_ml_rows(self, kind, origin, rows, **kw):
        self.pushed.append((kind, origin, [r.get("signal_ref") for r in rows]))
        return {"inserted": len(rows)}

    async def request_ml_evaluate(self, **kw):
        return {"result": {"re": {"use": False}}}


@pytest.mark.asyncio
class TestARunFromTheMac:

    @pytest.fixture
    def mac(self, dbs, monkeypatch):
        monkeypatch.setattr(service, "own_node_id", lambda: OWN)

    async def test_it_imports_theirs_sends_only_its_own_and_reports_both(self, mac):
        add_re(re_row("RE-MINE", 1.0))
        vps = _FakeVps([{**re_row("RE-THEIRS", 5_000_000.0), "id": 1}])

        got = await service.run_merge(vps)

        assert got["pulled"]["re"] == {"new": 1, "from": PEER}
        sent_refs = [ref for kind, _o, refs in vps.pushed if kind == "re" for ref in refs]
        assert sent_refs == ["RE-MINE"]
        assert all(origin == OWN for _k, origin, _r in vps.pushed)
        assert got["remote"] == {"re": {"use": False}}
        assert set(got["local"]) == {"re", "bo", "pro"}

    async def test_a_second_run_adds_nothing_new(self, mac):
        add_re(re_row("RE-MINE", 1.0))
        vps = _FakeVps([{**re_row("RE-THEIRS", 5_000_000.0), "id": 1}])
        await service.run_merge(vps)

        got = await service.run_merge(vps)

        assert got["pulled"]["re"]["new"] == 0
        assert mrepo.count("re") == 1

    async def test_an_older_vps_is_named_as_the_reason(self, mac):
        with pytest.raises(service.MergeFailed, match="older code"):
            await service.run_merge(_FakeVps([], old=True))
