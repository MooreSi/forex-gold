"""What the Mac is allowed to change on the VPS, and what happens when it can't.

`_handle_settings_propose` takes an `updates` dict off the sync channel and
writes it into `vantage_risk_settings` — risk per trade, max daily loss, max
open trades, whether signals auto-execute. It is the money path reached over a
network, and `_SYNCED_SETTINGS_KEYS` is the whole of what limits it.

Three properties, each silent when it goes wrong:

  * **The allowlist.** Anything outside it must be dropped rather than applied.
  * **The rejection reply.** A proposal that changes nothing must say so. The
    Mac holds unconfirmed changes and re-sends them on every reconnect until
    the VPS's confirmed snapshot reflects them — a proposal that is silently
    ignored is retried forever, on a link that reconnects every 15-90 seconds.
  * **`_from_sync=True`.** Without it, applying the Mac's proposal forwards it
    straight back over the same channel. The two nodes then echo one settings
    change at each other indefinitely.

Nothing here touches a real database.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import (
    MSG_SETTINGS_REJECTED, MSG_SETTINGS_STATE,
)

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def applied(monkeypatch):
    """Capture the call to update_risk_settings, kwargs included."""
    calls: list = []

    def _update(updates, **kw):
        calls.append((updates, kw))
        return updates
    monkeypatch.setattr(ss.db_module, "update_risk_settings", _update)
    return calls


@pytest.fixture
def node(monkeypatch, applied):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._clients = set()
    srv.broadcasts: list = []

    async def _broadcast(msg):
        srv.broadcasts.append(msg)
    srv._broadcast = _broadcast
    monkeypatch.setattr(ss.SyncServer, "_settings_snapshot",
                        lambda _s: {"max_open_trades": 5})
    return srv


class TestTheAllowlist:

    async def test_a_recognised_key_is_applied(self, node, applied):
        await node._handle_settings_propose(_Ws(), {"updates": {"max_open_trades": 3}})

        assert applied and applied[0][0] == {"max_open_trades": 3}

    # Checked against the real list rather than assumed: `profit_close_usd`
    # and `circuit_breaker_enabled` ARE synced, which my first draft of this
    # test got wrong. These four are genuinely outside it, and each is
    # something a peer setting remotely would be a problem.
    @pytest.mark.parametrize("key", [
        "trade_pause_until",     # would let a peer halt or un-halt trading
        "risk_halt_reason",      # the text shown to the operator when halted
        "starting_balance",      # what every P&L figure is measured against
        "harvest_pips",          # closed two live trades at the wrong threshold
    ])
    async def test_a_key_outside_the_list_is_not_applied(self, node, applied, key):
        ws = _Ws()

        await node._handle_settings_propose(ws, {"updates": {key: 999}})

        assert applied == [], f"a peer set {key!r} through the settings sync"
        assert ws.types() == [MSG_SETTINGS_REJECTED]

    async def test_the_recognised_half_of_a_mixed_proposal_still_applies(
            self, node, applied):
        await node._handle_settings_propose(_Ws(), {"updates": {
            "max_daily_loss_pct": 3.0,
            "trade_pause_until": 99999999,
        }})

        assert applied[0][0] == {"max_daily_loss_pct": 3.0}


class TestARejectionIsAlwaysAnswered:
    """The Mac re-sends unconfirmed changes on every reconnect. A proposal that
    is silently dropped is retried forever."""

    async def test_no_recognised_keys_gets_a_reason(self, node, applied):
        ws = _Ws()

        await node._handle_settings_propose(ws, {"updates": {"nonsense": 1}})

        assert ws.types() == [MSG_SETTINGS_REJECTED]
        assert "no recognised settings keys" in ws.sent[0]["reason"]

    async def test_an_empty_proposal_gets_a_reason(self, node, applied):
        ws = _Ws()

        await node._handle_settings_propose(ws, {"updates": {}})

        assert ws.types() == [MSG_SETTINGS_REJECTED]

    async def test_a_database_failure_gets_a_reason_rather_than_silence(
            self, node, monkeypatch):
        def _boom(updates, **kw):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(ss.db_module, "update_risk_settings", _boom)
        ws = _Ws()

        await node._handle_settings_propose(ws, {"updates": {"max_open_trades": 3}})

        assert ws.types() == [MSG_SETTINGS_REJECTED]
        assert "database is locked" in ws.sent[0]["reason"]

    async def test_a_failed_apply_does_NOT_broadcast_a_confirmation(self, node,
                                                                    monkeypatch):
        """A confirmed snapshot is how the Mac clears its pending change.
        Broadcasting one after a failure would tell the Mac a setting was
        applied that was not."""
        def _boom(updates, **kw):
            raise RuntimeError("nope")
        monkeypatch.setattr(ss.db_module, "update_risk_settings", _boom)

        await node._handle_settings_propose(_Ws(), {"updates": {"max_open_trades": 3}})

        assert node.broadcasts == []


class TestASuccessIsConfirmedBack:
    async def test_the_confirmed_snapshot_is_broadcast(self, node, applied):
        """Without this the Mac never learns its change landed, and re-sends it
        on every reconnect."""
        await node._handle_settings_propose(_Ws(), {"updates": {"max_open_trades": 3}})

        assert [m["type"] for m in node.broadcasts] == [MSG_SETTINGS_STATE]
        assert node.broadcasts[0]["settings"] == {"max_open_trades": 5}

    async def test_no_rejection_is_sent_on_success(self, node, applied):
        ws = _Ws()

        await node._handle_settings_propose(ws, {"updates": {"max_open_trades": 3}})

        assert ws.sent == []


class TestTheEchoLoopGuard:
    async def test_the_apply_is_marked_as_coming_FROM_sync(self, node, applied):
        """`update_risk_settings` forwards a change over sync unless told the
        change arrived that way. Without this flag, applying the Mac's proposal
        sends it straight back, and the two nodes echo one settings change at
        each other indefinitely."""
        await node._handle_settings_propose(_Ws(), {"updates": {"max_open_trades": 3}})

        assert applied[0][1].get("_from_sync") is True
