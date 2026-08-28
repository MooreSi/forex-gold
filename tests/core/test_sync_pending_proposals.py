"""The Mac's pending-proposal queue: settings changes that must not vanish.

`propose_settings()` was once fire-and-forget. If the link was not CONNECTED at
that instant, or dropped before the VPS replied, the change was silently lost.
On a link that reconnects every 15-90 seconds that was recurring, not an edge
case, and the comments in client.py record the user hitting it: a risk-settings
change that never reached the VPS and never retried.

Three separate mechanisms came out of that, and each is tested here because
each fails silently on its own:

  1. HOLD    -- a proposal is kept in _pending_* until the VPS's confirmed
                snapshot actually reflects it. Not until it is sent.
  2. PERSIST -- it is mirrored to app_config, so an app restart (or the object
                being recreated by a Local/Remote toggle) before confirmation
                does not lose it.
  3. SHIELD  -- the mirror-down path SKIPS keys with a pending change. Without
                it, a reconnect right after a VPS restart writes the VPS's
                stale value over the edit the user just made -- described in
                the source as "a real, observed failure mode".

Nothing here opens a socket. The websocket is a fake that records frames, and
app_config is a real database via fresh_db, since persistence is the point.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.cluster.sync.client import SyncClient
from backend.src.services.cluster.sync.protocol import (
    CONN_CONNECTED, CONN_DISCONNECTED,
    MSG_SETTINGS_PROPOSE, MSG_SETTINGS_STATE,
    MSG_CHANNEL_STRATEGY_STATE, MSG_TRADING_SCHEDULE_STATE,
    MSG_STRATEGY_PARAMS_STATE,
)


pytestmark = [pytest.mark.usefixtures("fresh_db"), pytest.mark.asyncio]


class _Ws:
    """Records what would have gone over the wire."""

    def __init__(self, fail: bool = False):
        self.sent: list[dict] = []
        self.fail = fail

    async def send(self, raw: str) -> None:
        if self.fail:
            raise ConnectionResetError("connection lost mid-send")
        self.sent.append(json.loads(raw))


@pytest.fixture
def offline():
    """A client with nothing pending and no connection."""
    c = SyncClient()
    c.conn_state = CONN_DISCONNECTED
    c._ws = None
    return c


def _connect(client) -> _Ws:
    ws = _Ws()
    client._ws = ws
    client.conn_state = CONN_CONNECTED
    return ws


def _stored(key: str):
    from backend.src.db import database as db_module
    raw = db_module.get_app_config(key)
    return json.loads(raw) if raw else None


class TestHoldUntilConfirmed:
    async def test_a_proposal_made_offline_is_held(self, offline):
        await offline.propose_settings({"risk_percent": 2.0})
        assert offline._pending_settings == {"risk_percent": 2.0}

    async def test_it_is_sent_once_connected(self, offline):
        await offline.propose_settings({"risk_percent": 2.0})
        ws = _connect(offline)

        await offline._flush_pending_settings()

        assert len(ws.sent) == 1
        assert ws.sent[0]["type"] == MSG_SETTINGS_PROPOSE
        assert ws.sent[0]["updates"] == {"risk_percent": 2.0}

    async def test_SENDING_IS_NOT_CONFIRMATION(self, offline):
        """The failure the queue exists for. A sent frame proves nothing --
        the VPS may never have applied it. It stays pending until the VPS's
        own snapshot says otherwise."""
        await offline.propose_settings({"risk_percent": 2.0})
        _connect(offline)

        await offline._flush_pending_settings()

        assert offline._pending_settings == {"risk_percent": 2.0}

    async def test_the_vps_snapshot_agreeing_clears_it(self, offline):
        await offline.propose_settings({"risk_percent": 2.0})

        await offline._dispatch({"type": MSG_SETTINGS_STATE,
                                 "settings": {"risk_percent": 2.0}})

        assert offline._pending_settings == {}

    async def test_a_snapshot_with_the_OLD_value_does_not_clear_it(self, offline):
        """A VPS that restarted with pre-change values. Clearing here loses
        the change permanently -- there would be nothing left to resend."""
        await offline.propose_settings({"risk_percent": 2.0})

        await offline._dispatch({"type": MSG_SETTINGS_STATE,
                                 "settings": {"risk_percent": 1.0}})

        assert offline._pending_settings == {"risk_percent": 2.0}

    async def test_only_the_confirmed_KEYS_clear(self, offline):
        """Per-key, not all-or-nothing. A partly-applied snapshot must not
        drop the parts still outstanding."""
        await offline.propose_settings({"risk_percent": 2.0, "max_trades": 5})

        await offline._dispatch({"type": MSG_SETTINGS_STATE,
                                 "settings": {"risk_percent": 2.0, "max_trades": 3}})

        assert offline._pending_settings == {"max_trades": 5}

    async def test_a_resend_carries_the_WHOLE_pending_set(self, offline):
        """Idempotent by design: a frame lost to a mid-flight disconnect is
        covered by the next resend rather than needing its own retry."""
        await offline.propose_settings({"risk_percent": 2.0})
        await offline.propose_settings({"max_trades": 5})
        ws = _connect(offline)

        await offline._flush_pending_settings()

        assert ws.sent[0]["updates"] == {"risk_percent": 2.0, "max_trades": 5}

    async def test_a_send_that_raises_keeps_the_proposal(self, offline):
        """A disconnect mid-send must not look like success."""
        await offline.propose_settings({"risk_percent": 2.0})
        offline._ws = _Ws(fail=True)
        offline.conn_state = CONN_CONNECTED

        await offline._flush_pending_settings()          # must not raise

        assert offline._pending_settings == {"risk_percent": 2.0}

    async def test_nothing_is_sent_while_disconnected(self, offline):
        ws = _Ws()
        offline._ws = ws
        offline.conn_state = CONN_DISCONNECTED

        await offline.propose_settings({"risk_percent": 2.0})

        assert ws.sent == []

    async def test_flushing_an_empty_queue_sends_nothing(self, offline):
        ws = _connect(offline)
        await offline._flush_pending_settings()
        assert ws.sent == []


class TestItSurvivesARestart:
    """The queue lives in app_config, not just memory. A Local/Remote toggle
    recreates this object."""

    async def test_a_pending_proposal_is_written_to_app_config(self, offline):
        await offline.propose_settings({"risk_percent": 2.0})
        assert _stored("sync_pending_settings") == {"risk_percent": 2.0}

    async def test_a_NEW_client_picks_it_back_up(self, offline):
        await offline.propose_settings({"risk_percent": 2.0})

        reborn = SyncClient()

        assert reborn._pending_settings == {"risk_percent": 2.0}

    async def test_and_still_sends_it(self, offline):
        await offline.propose_settings({"risk_percent": 2.0})
        reborn = SyncClient()
        ws = _connect(reborn)

        await reborn._flush_pending_settings()

        assert ws.sent[0]["updates"] == {"risk_percent": 2.0}

    async def test_clearing_is_persisted_too(self, offline):
        """Otherwise a restart resurrects a change the VPS already applied,
        re-proposing it over whatever the user has set since."""
        await offline.propose_settings({"risk_percent": 2.0})
        await offline._dispatch({"type": MSG_SETTINGS_STATE,
                                 "settings": {"risk_percent": 2.0}})

        assert SyncClient()._pending_settings == {}

    async def test_a_corrupt_stored_value_is_ignored_not_fatal(self):
        """This runs in __init__. Raising here means no sync client at all."""
        from backend.src.db import database as db_module
        db_module.set_app_config("sync_pending_settings", "{not json")

        assert SyncClient()._pending_settings == {}


class TestTheMirrorDownShield:
    """The VPS's snapshot must not overwrite an edit that has not reached it."""

    async def test_a_pending_key_is_NOT_written_locally(self, offline, monkeypatch):
        """The observed failure: reconnect after a VPS restart, and the stale
        value lands on top of the user's just-made change."""
        from backend.src.db import database as db_module
        wrote: list = []
        monkeypatch.setattr(db_module, "update_risk_settings",
                            lambda s, **kw: wrote.append(s))

        await offline.propose_settings({"risk_percent": 2.0})
        offline._mirror_settings_locally({"risk_percent": 1.0})

        assert wrote == [], "the VPS's stale value overwrote a pending change"

    async def test_other_keys_still_mirror(self, offline, monkeypatch):
        """The shield is per-key. Blocking the whole snapshot would stop
        Local mode starting from the same baseline."""
        from backend.src.db import database as db_module
        wrote: list = []
        monkeypatch.setattr(db_module, "update_risk_settings",
                            lambda s, **kw: wrote.append(s))

        await offline.propose_settings({"risk_percent": 2.0})
        offline._mirror_settings_locally({"risk_percent": 1.0, "max_trades": 3})

        assert wrote == [{"max_trades": 3}]

    async def test_with_nothing_pending_everything_mirrors(self, offline, monkeypatch):
        from backend.src.db import database as db_module
        wrote: list = []
        monkeypatch.setattr(db_module, "update_risk_settings",
                            lambda s, **kw: wrote.append(s))

        offline._mirror_settings_locally({"risk_percent": 1.0})

        assert wrote == [{"risk_percent": 1.0}]

    async def test_it_is_marked_as_coming_FROM_SYNC(self, offline, monkeypatch):
        """_from_sync stops the write being forwarded straight back to the
        VPS as a new local proposal."""
        from backend.src.db import database as db_module
        seen: dict = {}
        monkeypatch.setattr(db_module, "update_risk_settings",
                            lambda s, **kw: seen.update(kw))

        offline._mirror_settings_locally({"risk_percent": 1.0})

        assert seen.get("_from_sync") is True


class TestTheOtherThreeQueues:
    """Channel strategy, trading schedule and strategy params repeat the
    pattern with different shapes. Each is confirmed differently."""

    async def test_channel_strategy_clears_only_on_a_full_match(self, offline):
        """Both fields must agree. Strategy right but auto wrong is not the
        change the user asked for."""
        await offline.propose_channel_strategy("Gold Diggers VIP", "scalp", True)

        await offline._dispatch({"type": MSG_CHANNEL_STRATEGY_STATE,
                                 "channel_strategy": {"Gold Diggers VIP":
                                                      {"strategy": "scalp", "auto": False}}})
        assert "Gold Diggers VIP" in offline._pending_channel_strategy

        await offline._dispatch({"type": MSG_CHANNEL_STRATEGY_STATE,
                                 "channel_strategy": {"Gold Diggers VIP":
                                                      {"strategy": "scalp", "auto": True}}})
        assert offline._pending_channel_strategy == {}

    async def test_channel_strategy_survives_a_restart(self, offline):
        await offline.propose_channel_strategy("Gold Diggers VIP", "scalp", True)
        assert SyncClient()._pending_channel_strategy == {
            "Gold Diggers VIP": {"strategy": "scalp", "auto": True}}

    async def test_the_trading_schedule_is_one_atomic_snapshot(self, offline):
        """Edited and saved as a single 7-day unit in the UI, so it clears
        only when the whole snapshot matches."""
        snap = {"enabled": True, "schedule": {"mon": ["08:00-12:00"]}}
        await offline.propose_trading_schedule(snap)

        await offline._dispatch({"type": MSG_TRADING_SCHEDULE_STATE,
                                 "trading_schedule": {"enabled": True, "schedule": {}}})
        assert offline._pending_trading_schedule == snap

        await offline._dispatch({"type": MSG_TRADING_SCHEDULE_STATE,
                                 "trading_schedule": snap})
        assert offline._pending_trading_schedule is None

    async def test_strategy_params_clear_only_on_a_whole_snapshot_match(self, offline):
        snap = {"scalp": {"tp": 3.0}}
        await offline.propose_strategy_params(snap)

        await offline._dispatch({"type": MSG_STRATEGY_PARAMS_STATE,
                                 "strategy_params": {"scalp": {"tp": 4.0}}})
        assert offline._pending_strategy_params == snap

        await offline._dispatch({"type": MSG_STRATEGY_PARAMS_STATE,
                                 "strategy_params": snap})
        assert offline._pending_strategy_params is None

    async def test_a_cleared_single_snapshot_persists_as_empty_not_null_text(self, offline):
        """These two store "" rather than "null" when cleared, and _load
        reads a falsy raw as None. Writing "null" would round-trip to the
        string, which is truthy, and resend a change already applied."""
        from backend.src.db import database as db_module
        await offline.propose_trading_schedule({"enabled": True})
        await offline._dispatch({"type": MSG_TRADING_SCHEDULE_STATE,
                                 "trading_schedule": {"enabled": True}})

        assert db_module.get_app_config("sync_pending_trading_schedule") == ""
        assert SyncClient()._pending_trading_schedule is None
