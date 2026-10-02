"""The Dashboard's Fill cost and GEX cards follow the node that trades.

Owner, 2026-10-02: "it should always show the trading node". Both cards read
the local database, so on the Mac, with the VPS trading, they described a
machine that places no orders. The Mac now asks the VPS for the same report
and shows that; the VPS answers from its own database.

**Nothing here reaches a broker.** The reports are read-only summaries of
tables that already exist; the sockets and report functions are recorders.

What each layer must never do:

  * the Mac must never show its OWN fills in place of the trader's when the
    VPS is the trader and unreachable: that is the exact misreport this fixes,
    so it says "unreachable" instead;
  * the VPS must never run a report it was not asked for by name: the name
    comes off the wire, so it is looked up in a fixed table, not imported;
  * an answer to some other request must never be taken as this one's.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.db import database as db_module
from backend.src.services.broker import fill_cost_report
from backend.src.services.cluster import peer_reports as pr
from backend.src.services.cluster import remote_control as rc
from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import TRADER_LOCAL, TRADER_REMOTE_VPS
from backend.src.services.market import gex_report


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.fixture
def local_reports(monkeypatch):
    """Recorders in place of the two reports, in the table the VPS looks up."""
    calls: list[tuple] = []

    async def _fill(days=14):
        calls.append(("fill_cost", days))
        return {"n": 3, "days": days}

    async def _gex():
        calls.append(("gex",))
        return {"snapshot": None, "n_snapshots": 0}

    monkeypatch.setitem(pr.REPORTS, "fill_cost", _fill)
    monkeypatch.setitem(pr.REPORTS, "gex", _gex)
    return calls


# ── the VPS side ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTheVpsSide:
    async def test_it_runs_its_own_report_and_answers(self, local_reports):
        ws = _Ws()

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            ws, {"type": P.MSG_PEER_REPORT, "req_id": "r1", "name": "fill_cost",
                 "args": {"days": 7}})

        assert local_reports == [("fill_cost", 7)]
        assert ws.sent == [{"type": P.MSG_PEER_REPORT_ACK, "req_id": "r1",
                            "result": {"n": 3, "days": 7}}]

    async def test_a_name_it_does_not_know_is_refused_and_runs_nothing(self, local_reports):
        ws = _Ws()

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            ws, {"type": P.MSG_PEER_REPORT, "req_id": "r2",
                 "name": "os.system", "args": {}})

        assert local_reports == []
        assert ws.sent[0]["req_id"] == "r2"
        assert "os.system" in ws.sent[0]["error"]
        assert "result" not in ws.sent[0]

    async def test_arguments_it_does_not_take_are_refused(self, local_reports):
        ws = _Ws()

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            ws, {"type": P.MSG_PEER_REPORT, "req_id": "r3", "name": "gex",
                 "args": {"days": 7}})

        assert local_reports == []
        assert "error" in ws.sent[0]

    async def test_a_failure_comes_back_as_an_error(self, monkeypatch):
        async def _boom(days=14):
            raise RuntimeError("database is locked")
        monkeypatch.setitem(pr.REPORTS, "fill_cost", _boom)
        ws = _Ws()

        await ss.SyncServer.__new__(ss.SyncServer)._dispatch(
            ws, {"type": P.MSG_PEER_REPORT, "req_id": "r4", "name": "fill_cost",
                 "args": {}})

        assert "database is locked" in ws.sent[0]["error"]
        assert "result" not in ws.sent[0]

    async def test_the_reports_are_the_real_ones(self):
        """The table must point at the functions the cards used to call."""
        assert pr.REPORTS["fill_cost"] is fill_cost_report.report_async
        assert pr.REPORTS["gex"] is gex_report.report_async


# ── the Mac side ─────────────────────────────────────────────────────────────

class _MacWs:
    def __init__(self, client, reply=None, wrong_id=False):
        self.client, self.reply, self.wrong_id = client, reply, wrong_id
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.reply is not None and msg["type"] == P.MSG_PEER_REPORT:
            ack = {**self.reply, "type": P.MSG_PEER_REPORT_ACK,
                   "req_id": "someone-else" if self.wrong_id else msg["req_id"]}
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.client._dispatch(ack)))


def _mac(reply=None, connected=True, wrong_id=False):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = P.CONN_CONNECTED if connected else P.CONN_DISCONNECTED
    cli._ws = _MacWs(cli, reply, wrong_id)
    return cli


@pytest.mark.asyncio
class TestTheMacSide:
    async def test_it_asks_by_name_and_returns_the_answer(self):
        cli = _mac(reply={"result": {"n": 9}})

        got = await cli.request_peer_report("fill_cost", {"days": 14}, timeout=2)

        sent = cli._ws.sent[0]
        assert (sent["type"], sent["name"], sent["args"]) == (
            P.MSG_PEER_REPORT, "fill_cost", {"days": 14})
        assert sent["req_id"]
        assert got["result"] == {"n": 9}

    async def test_not_connected_sends_nothing(self):
        cli = _mac(connected=False)

        with pytest.raises(ConnectionError):
            await cli.request_peer_report("gex", {}, timeout=2)
        assert cli._ws.sent == []

    async def test_an_answer_to_another_request_is_not_taken_as_this_ones(self):
        cli = _mac(reply={"result": {"n": 9}}, wrong_id=True)

        with pytest.raises(asyncio.TimeoutError):
            await cli.request_peer_report("gex", {}, timeout=0.3)


# ── which node answers ───────────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.asked: list[tuple] = []

    async def request_peer_report(self, name, args, **kw):
        self.asked.append((name, args))
        if self.raises:
            raise self.raises
        return self.reply


@pytest.fixture
def mac_trading_on_vps(monkeypatch):
    holder = {"client": _FakeClient(reply={"result": {"n": 41, "days": 14}})}
    monkeypatch.setattr(pr, "trader_is_peer", lambda: True)
    monkeypatch.setattr(pr._client, "get_instance", lambda: holder["client"])
    return holder


@pytest.mark.asyncio
class TestOnTradingNode:
    async def test_the_vps_trading_means_the_vps_answers_not_this_node(
            self, mac_trading_on_vps, local_reports):
        got = await pr.on_trading_node("fill_cost", days=14)

        assert got == {"n": 41, "days": 14, "node": "remote"}
        assert mac_trading_on_vps["client"].asked == [("fill_cost", {"days": 14})]
        assert local_reports == [], "this node's own fills must not be read"

    async def test_gex_asks_with_no_arguments(self, mac_trading_on_vps, local_reports):
        await pr.on_trading_node("gex")

        assert mac_trading_on_vps["client"].asked == [("gex", {})]

    async def test_this_node_trading_means_its_own_report(self, monkeypatch, local_reports):
        monkeypatch.setattr(pr, "trader_is_peer", lambda: False)

        got = await pr.on_trading_node("fill_cost", days=7)

        assert got == {"n": 3, "days": 7, "node": "local"}
        assert local_reports == [("fill_cost", 7)]

    async def test_unreachable_trader_is_said_and_never_replaced_by_local_data(
            self, mac_trading_on_vps, local_reports):
        mac_trading_on_vps["client"] = _FakeClient(
            raises=ConnectionError("not connected to VPS"))

        with pytest.raises(rc.RemoteControlFailed, match="could not be reached"):
            await pr.on_trading_node("fill_cost", days=14)
        assert local_reports == []

    async def test_no_answer_is_said_and_never_replaced_by_local_data(
            self, mac_trading_on_vps, local_reports):
        mac_trading_on_vps["client"] = _FakeClient(raises=asyncio.TimeoutError())

        with pytest.raises(rc.RemoteControlFailed, match="did not answer"):
            await pr.on_trading_node("gex")
        assert local_reports == []

    async def test_the_vps_reason_is_passed_on(self, mac_trading_on_vps, local_reports):
        mac_trading_on_vps["client"] = _FakeClient(reply={"error": "database is locked"})

        with pytest.raises(rc.RemoteControlFailed, match="database is locked"):
            await pr.on_trading_node("gex")
        assert local_reports == []

    async def test_an_older_vps_that_knows_no_such_message_times_out_and_says_so(
            self, mac_trading_on_vps):
        mac_trading_on_vps["client"] = _FakeClient(raises=asyncio.TimeoutError())

        with pytest.raises(rc.RemoteControlFailed, match="older"):
            await pr.on_trading_node("gex")


# ── who is trading ───────────────────────────────────────────────────────────

class TestTraderIsPeer:
    @pytest.fixture
    def mac(self, monkeypatch):
        """A paired Mac: a VPS host is configured and no sync server runs here."""
        monkeypatch.setattr(sc.SyncClient, "load_config",
                            staticmethod(lambda: ("vps.example", 8765, "t")))
        monkeypatch.setattr(ss, "get_instance", lambda: None)

    def test_vps_is_the_active_trader(self, mac, monkeypatch):
        monkeypatch.setattr(db_module, "get_active_trader", lambda: TRADER_REMOTE_VPS)
        assert pr.trader_is_peer() is True

    def test_this_node_is_the_active_trader(self, mac, monkeypatch):
        monkeypatch.setattr(db_module, "get_active_trader", lambda: TRADER_LOCAL)
        assert pr.trader_is_peer() is False

    def test_an_unpaired_install_is_its_own_trader(self, monkeypatch):
        monkeypatch.setattr(sc.SyncClient, "load_config",
                            staticmethod(lambda: ("", 8765, "")))
        monkeypatch.setattr(ss, "get_instance", lambda: None)
        monkeypatch.setattr(db_module, "get_active_trader", lambda: TRADER_REMOTE_VPS)
        assert pr.trader_is_peer() is False

    def test_the_vps_itself_is_never_its_own_peer(self, monkeypatch):
        monkeypatch.setattr(sc.SyncClient, "load_config",
                            staticmethod(lambda: ("vps.example", 8765, "t")))
        monkeypatch.setattr(ss, "get_instance", lambda: object())
        monkeypatch.setattr(db_module, "get_active_trader", lambda: TRADER_REMOTE_VPS)
        assert pr.trader_is_peer() is False
