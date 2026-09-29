"""Close a position the VPS opened, from the Mac (owner, 2026-09-29).

"on the local node under trading > positions i'm unable to press the close
button ... i should still be able to close the button on the local node it
should send the command to the vps". Both nodes trade one MT5 account, but the
record of a VPS trade is in the VPS's database, so only the VPS can close it
against that record. The Mac asks; the VPS runs its own `close_trade` with the
two positional arguments the HTTP route would have passed, unchanged (golden
rule 2); the answer comes back.

**Nothing here reaches a broker.** The VPS runtime is a recorder whose
`close_trade` returns a canned dict or raises; the sockets are recorders. No
bridge, no MT5 binding, no real close -- asserted by
`test_no_test_in_this_file_can_reach_a_real_close`.

What each layer must never do:

  * the VPS side must never reshape the close (argument added, reordered or
    defaulted differently from `TradingRuntime.close_trade`);
  * the Mac side must never fall back to closing anything locally, and never
    call an unanswered request "nothing closed": the VPS may have closed it.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from backend.src.services.cluster import remote_control as rc
from backend.src.services.cluster.sync import client as sc
from backend.src.services.cluster.sync import protocol as P
from backend.src.services.cluster.sync import server as ss

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _VpsRuntime:
    """Records `close_trade` calls. Holds no bridge."""

    def __init__(self, result=None, raises=None):
        self.calls: list[tuple] = []
        self._result = result if result is not None else {"trade_id": "T-7", "pnl": 12.5}
        self._raises = raises

    async def close_trade(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self._raises:
            raise self._raises
        return self._result


def _vps(runtime):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._main_engine = runtime
    return srv


async def test_no_test_in_this_file_can_reach_a_real_close():
    assert not hasattr(_VpsRuntime(), "_bridge")
    assert not hasattr(_VpsRuntime(), "_make_close_trade_ctx")


# ── the VPS side ─────────────────────────────────────────────────────────────

class TestTheVpsSide:
    async def test_it_closes_with_its_own_close_trade_positionally_and_answers(self):
        runtime, ws = _VpsRuntime(), _Ws()

        await _vps(runtime)._dispatch(ws, {
            "type": P.MSG_CLOSE_TRADE, "req_id": "r1",
            "trade_id": "T-7", "reason": "manual_close"})

        assert runtime.calls == [(("T-7", "manual_close"), {})]
        assert ws.sent == [{"type": P.MSG_CLOSE_TRADE_ACK, "req_id": "r1",
                            "result": {"trade_id": "T-7", "pnl": 12.5}}]

    async def test_the_reason_is_passed_through_not_defaulted(self):
        runtime = _VpsRuntime()

        await _vps(runtime)._dispatch(_Ws(), {
            "type": P.MSG_CLOSE_TRADE, "req_id": "r1",
            "trade_id": "T-7", "reason": "operator_closed"})

        assert runtime.calls[0][0] == ("T-7", "operator_closed")

    async def test_a_refusal_comes_back_in_the_vps_own_words(self):
        runtime = _VpsRuntime(raises=ValueError("Trade T-7 is not open"))
        ws = _Ws()

        await _vps(runtime)._dispatch(ws, {
            "type": P.MSG_CLOSE_TRADE, "req_id": "r2",
            "trade_id": "T-7", "reason": "manual_close"})

        assert ws.sent[0]["req_id"] == "r2"
        assert ws.sent[0]["error"] == "Trade T-7 is not open"
        assert "result" not in ws.sent[0]

    async def test_an_mt5_rejection_comes_back_too(self):
        runtime = _VpsRuntime(raises=RuntimeError("MT5 close rejected for ticket 5"))
        ws = _Ws()

        await _vps(runtime)._dispatch(ws, {
            "type": P.MSG_CLOSE_TRADE, "req_id": "r3",
            "trade_id": "T-7", "reason": "manual_close"})

        assert "MT5 close rejected" in ws.sent[0]["error"]

    async def test_no_runtime_closes_nothing_and_says_why(self):
        ws = _Ws()

        await _vps(None)._dispatch(ws, {
            "type": P.MSG_CLOSE_TRADE, "req_id": "r4",
            "trade_id": "T-7", "reason": "manual_close"})

        assert ws.sent[0]["req_id"] == "r4"
        assert ws.sent[0]["error"]

    async def test_a_request_without_a_trade_id_closes_nothing(self):
        runtime, ws = _VpsRuntime(), _Ws()

        await _vps(runtime)._dispatch(ws, {
            "type": P.MSG_CLOSE_TRADE, "req_id": "r5", "reason": "manual_close"})

        assert runtime.calls == []
        assert ws.sent[0]["error"]

    async def test_the_vps_handler_matches_the_runtimes_close_signature(self):
        """Two positionals, (trade_id, reason). If the runtime's close ever
        changes shape, this forward must be revisited, not silently kept."""
        from backend.src.runtime import TradingRuntime
        params = list(inspect.signature(TradingRuntime.close_trade).parameters)
        assert params == ["self", "trade_id", "reason"]


# ── the Mac side ─────────────────────────────────────────────────────────────

class _MacWs:
    """Answers a close request with `reply`, echoing its req_id unless
    `wrong_id` is set."""

    def __init__(self, client, reply=None, wrong_id=False):
        self.client = client
        self.reply = reply
        self.wrong_id = wrong_id
        self.sent: list[dict] = []

    async def send(self, raw):
        msg = json.loads(raw)
        self.sent.append(msg)
        if self.reply is not None and msg["type"] == P.MSG_CLOSE_TRADE:
            ack = {**self.reply, "type": P.MSG_CLOSE_TRADE_ACK,
                   "req_id": "someone-else" if self.wrong_id else msg["req_id"]}
            asyncio.get_running_loop().call_soon(
                lambda: asyncio.ensure_future(self.client._dispatch(ack)))


def _mac(reply=None, connected=True, wrong_id=False):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli.conn_state = P.CONN_CONNECTED if connected else P.CONN_DISCONNECTED
    cli._ws = _MacWs(cli, reply, wrong_id)
    return cli


class TestTheMacSide:
    async def test_it_sends_the_trade_and_reason_and_returns_the_answer(self):
        cli = _mac(reply={"result": {"trade_id": "T-7"}})

        got = await cli.request_peer_close(trade_id="T-7", reason="manual_close", timeout=2)

        sent = cli._ws.sent[0]
        assert sent["type"] == P.MSG_CLOSE_TRADE
        assert (sent["trade_id"], sent["reason"]) == ("T-7", "manual_close")
        assert sent["req_id"]
        assert got["result"] == {"trade_id": "T-7"}

    async def test_not_connected_sends_nothing(self):
        cli = _mac(connected=False)

        with pytest.raises(ConnectionError):
            await cli.request_peer_close(trade_id="T-7", reason="manual_close", timeout=2)
        assert cli._ws.sent == []

    async def test_an_answer_to_another_request_is_not_taken_as_this_ones(self):
        cli = _mac(reply={"result": {"trade_id": "OTHER"}}, wrong_id=True)

        with pytest.raises(asyncio.TimeoutError):
            await cli.request_peer_close(trade_id="T-7", reason="manual_close", timeout=0.3)

    async def test_a_vps_that_never_answers_times_out(self):
        cli = _mac(reply=None)

        with pytest.raises(asyncio.TimeoutError):
            await cli.request_peer_close(trade_id="T-7", reason="manual_close", timeout=0.2)


# ── what the operator is told ────────────────────────────────────────────────

class _FakeClient:
    def __init__(self, reply=None, raises=None):
        self.reply, self.raises = reply, raises
        self.calls: list[dict] = []

    async def request_peer_close(self, **kw):
        self.calls.append(kw)
        if self.raises:
            raise self.raises
        return self.reply


@pytest.fixture
def peer(monkeypatch):
    holder = {"client": _FakeClient(reply={"result": {"trade_id": "T-7"}})}
    monkeypatch.setattr(rc._client, "get_instance", lambda: holder["client"])
    return holder


class TestCloseOnPeer:
    async def test_success_says_it_happened_on_the_remote_node(self, peer):
        got = await rc.close_on_peer("T-7", "manual_close")

        assert peer["client"].calls == [{"trade_id": "T-7", "reason": "manual_close"}]
        assert got == {"trade_id": "T-7", "where": "remote"}

    async def test_a_refusal_is_the_vps_reason(self, peer):
        peer["client"] = _FakeClient(reply={"error": "Trade T-7 is not open"})

        with pytest.raises(rc.RemoteControlFailed, match="Trade T-7 is not open"):
            await rc.close_on_peer("T-7", "manual_close")

    async def test_unreachable_says_nothing_was_closed(self, peer):
        peer["client"] = _FakeClient(raises=ConnectionError("not connected to VPS"))

        with pytest.raises(rc.RemoteControlFailed, match="Nothing was closed"):
            await rc.close_on_peer("T-7", "manual_close")

    async def test_no_answer_never_claims_nothing_was_closed(self, peer):
        """The request went out. The VPS may have closed it and lost the ack;
        telling the operator "nothing closed" invites a second close."""
        peer["client"] = _FakeClient(raises=asyncio.TimeoutError())

        with pytest.raises(rc.RemoteControlFailed) as exc:
            await rc.close_on_peer("T-7", "manual_close")
        assert "Nothing was closed" not in str(exc.value)
        assert "may have closed" in str(exc.value)
