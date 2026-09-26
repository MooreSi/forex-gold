"""Every risk setting reaches the other node, unless it is named as per-node.

The VPS applies a settings proposal only for keys in `_SYNCED_SETTINGS_KEYS`.
For months that was an allowlist added to one gap at a time -- its own comments
are a run of "same gap again, found 2026-07-07" -- and by 2026-09-25 about 65
of ~118 columns were missing. The Mac queued 38 changes the VPS had silently
dropped, including `setforget_lot_size`, the give-back guard and every Telegram
parsing switch, while its own screen showed them as set.

The owner's call (2026-09-25): sync everything except a named per-node list.
The list stays explicit, because it is also what keeps a key off the network
out of the SQL (see tests/utils/test_sql_identifiers.py). What makes it
complete is the first test here: a column nobody classified fails the suite.

Three properties:

  * **Complete.** Every column of a freshly migrated `vantage_risk_settings`
    is either synced or per-node, never neither and never both.
  * **Real.** Every synced key is an actual column, so a stale name (for
    example `gdc_live_execution`, which no code reads) is not applied.
  * **Cleared.** A key the VPS will not apply is named in its reply, and the
    Mac drops it from the queue instead of re-sending it on every reconnect.

Nothing here opens a socket or reaches a broker.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.client import SyncClient
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_SETTINGS_REJECTED,
)
from backend.src.services.cluster.sync.synced_settings import PER_NODE_SETTINGS


def _columns(db) -> set[str]:
    with db.db() as conn:
        rows = conn.execute("PRAGMA table_info(vantage_risk_settings)").fetchall()
    return {r[1] for r in rows} - {"id"}


class TestTheListIsComplete:
    def test_every_column_is_synced_or_named_per_node(self, fresh_db):
        unclassified = _columns(fresh_db) - set(ss._SYNCED_SETTINGS_KEYS) - PER_NODE_SETTINGS

        assert not unclassified, (
            "these settings reach neither node's peer and nothing says why: "
            f"{sorted(unclassified)}. Add each to _SYNCED_SETTINGS_KEYS, or to "
            "PER_NODE_SETTINGS with the reason it must differ per machine.")

    def test_no_setting_is_both(self):
        assert not set(ss._SYNCED_SETTINGS_KEYS) & PER_NODE_SETTINGS

    def test_every_synced_key_is_a_real_column(self, fresh_db):
        assert set(ss._SYNCED_SETTINGS_KEYS) - _columns(fresh_db) == set()

    def test_the_trading_clock_stays_per_node(self):
        """One-way on purpose: the VPS adopts the Mac's clock from its pings.
        As an ordinary synced setting the broadcast would push the VPS's value
        back and the Mac would run on its server's clock."""
        assert "trading_clock_offset_min" in PER_NODE_SETTINGS


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


@pytest.fixture
def node(monkeypatch):
    applied: list = []
    monkeypatch.setattr(ss.db_module, "update_risk_settings",
                        lambda updates, **kw: applied.append(updates))
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._clients = set()
    srv.broadcasts = []

    async def _broadcast(msg):
        srv.broadcasts.append(msg)
    srv._broadcast = _broadcast
    monkeypatch.setattr(ss.SyncServer, "_settings_snapshot", lambda _s: {})
    srv.applied = applied
    return srv


@pytest.mark.asyncio
class TestTheQueuedChangesApply:
    # A sample of the 38 the Mac had queued on 2026-09-25, chosen for what
    # they do to the trading node: size, protect, filter, parse.
    @pytest.mark.parametrize("key,value", [
        ("setforget_lot_size", 0.05),
        ("giveback_guard_enabled", 1),
        ("htf_bias_gate_enabled", 1),
        ("lk_enable_sl_parsing", 0),
        ("default_lot_size", 0.02),
        ("allow_no_sl", 0),
    ])
    async def test_the_vps_applies_it(self, node, key, value):
        await node._handle_settings_propose(_Ws(), {"updates": {key: value}})

        assert node.applied == [{key: value}]

    async def test_a_key_it_will_not_apply_is_named_in_the_reply(self, node):
        ws = _Ws()

        await node._handle_settings_propose(ws, {"updates": {
            "max_open_trades": 3, "gdc_live_execution": 1,
            "trading_clock_offset_min": 60}})

        assert node.applied == [{"max_open_trades": 3}]
        rejected = [m for m in ws.sent if m["type"] == MSG_SETTINGS_REJECTED]
        assert rejected and sorted(rejected[0]["keys"]) == [
            "gdc_live_execution", "trading_clock_offset_min"]


@pytest.mark.usefixtures("fresh_db")
@pytest.mark.asyncio
class TestTheMacStopsResending:
    async def test_a_named_key_leaves_the_queue_and_the_rest_stay(self):
        client = SyncClient()
        client.conn_state = CONN_CONNECTED
        client._ws = _Ws()
        await client.propose_settings({"gdc_live_execution": 1, "max_open_trades": 3})

        await client._dispatch({
            "type": MSG_SETTINGS_REJECTED, "reason": "not synced",
            "keys": ["gdc_live_execution"]})

        assert client._pending_settings == {"max_open_trades": 3}
        assert json.loads(fresh_app_config("sync_pending_settings")) == {
            "max_open_trades": 3}

    async def test_a_rejection_without_keys_keeps_the_queue(self):
        """An older VPS sends no key list. Guessing would drop real changes."""
        client = SyncClient()
        client._pending_settings = {"max_open_trades": 3}

        await client._dispatch({"type": MSG_SETTINGS_REJECTED,
                                      "reason": "database is locked"})

        assert client._pending_settings == {"max_open_trades": 3}


def fresh_app_config(key: str) -> str:
    from backend.src.db import database as db_module
    return db_module.get_app_config(key)
