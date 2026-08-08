"""SyncServer._status_payload() now includes ea_connected, so a paired
node's EA state is visible to the other node via the existing 3s heartbeat
-- no separate polling mechanism needed for the top-bar badge to stay live
across a Local/Remote pairing."""
import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

from backend.src.services.broker import ea_bridge as ea_bridge
from backend.src.services.cluster.sync.server import SyncServer


class _FakeEngine:
    def get_open_trades(self):
        return []

    async def get_mt5_account(self):
        return None


def test_heartbeat_includes_ea_connected_true():
    ea_bridge.set_instance(None)
    try:
        fake_ea = MagicMock()
        fake_ea.is_ea_healthy.return_value = True
        ea_bridge.set_instance(fake_ea)

        server = SyncServer(main_engine=_FakeEngine())
        payload = asyncio.run(server._status_payload())
        assert payload["ea_connected"] is True
    finally:
        ea_bridge.set_instance(None)


def test_heartbeat_includes_ea_connected_false_when_no_ea():
    ea_bridge.set_instance(None)
    server = SyncServer(main_engine=_FakeEngine())
    payload = asyncio.run(server._status_payload())
    assert payload["ea_connected"] is False
