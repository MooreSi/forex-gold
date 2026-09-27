"""A queued channel-strategy change for a channel the VPS does not have is let go.

The Mac holds a channel-strategy proposal until the VPS's snapshot echoes it
back. The VPS builds that snapshot from its own configured channels only, so
an entry for a channel it lacks is never echoed and was re-sent on every
reconnect, for good. 2026-09-26, the Mac's queue held two:

- 'Gold Diggers 2.0' -- the pre-rename name of GOLD DIGGERS INSTITUTIONAL,
  which no longer exists on either machine;
- 'Gold Diggers Scalping' -- the Mac's third Telegram slot. The VPS has two.

Every reconnect logged "resending 2 unconfirmed local channel strategy
change(s)" and the VPS wrote both rows again. The VPS has applied the value
by the time it answers (the row is written; it is just not a channel it
lists), so the proposal has done all it can.
"""
from __future__ import annotations

import json

import pytest

from backend.src.db import database as db_module
from backend.src.services.cluster.sync.client import SyncClient
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, MSG_CHANNEL_STRATEGY_STATE,
)

pytestmark = [pytest.mark.usefixtures("fresh_db"), pytest.mark.asyncio]


class _Ws:
    def __init__(self):
        self.sent: list[dict] = []

    async def send(self, raw: str) -> None:
        self.sent.append(json.loads(raw))


@pytest.fixture
def online():
    c = SyncClient()
    c._ws = _Ws()
    c.conn_state = CONN_CONNECTED
    return c


_VPS_SNAPSHOT = {
    "Gold Diggers VIP": {"strategy": None, "auto": False},
    "GOLD DIGGERS INSTITUTIONAL": {"strategy": "template:Test Template", "auto": False},
}


async def test_a_channel_missing_from_the_vps_snapshot_is_dropped(online):
    await online.propose_channel_strategy(
        "Gold Diggers Scalping", "template:30 TP1 SL50 and Trail", False)
    assert "Gold Diggers Scalping" in online._pending_channel_strategy

    await online._dispatch({"type": MSG_CHANNEL_STRATEGY_STATE,
                            "channel_strategy": _VPS_SNAPSHOT})

    assert online._pending_channel_strategy == {}
    assert json.loads(db_module.get_app_config("sync_pending_channel_strategy")) == {}


async def test_a_listed_channel_that_disagrees_is_still_held(online):
    """The rule it must not loosen: the VPS has the channel, and its value is
    not the one asked for."""
    await online.propose_channel_strategy("Gold Diggers VIP", "scalp", True)

    await online._dispatch({"type": MSG_CHANNEL_STRATEGY_STATE,
                            "channel_strategy": _VPS_SNAPSHOT})

    assert "Gold Diggers VIP" in online._pending_channel_strategy


async def test_only_the_missing_one_goes(online):
    await online.propose_channel_strategy("Gold Diggers VIP", "scalp", True)
    await online.propose_channel_strategy("Gold Diggers 2.0", "template:Test Template", False)

    await online._dispatch({"type": MSG_CHANNEL_STRATEGY_STATE,
                            "channel_strategy": _VPS_SNAPSHOT})

    assert list(online._pending_channel_strategy) == ["Gold Diggers VIP"]
