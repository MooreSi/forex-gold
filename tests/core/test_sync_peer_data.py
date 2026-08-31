"""What one node lets the other write into it.

`ServerPeerDataMixin` handles the messages that carry DATA rather than
commands: a parser rule the Mac approved, the AI review queue, and the AI
provider config. Each one takes something a peer sent and writes it into this
node's database or config file.

The one that matters most is `_handle_ai_config_sync`, because it writes to
`config.yaml` — the same file that holds the bridge URL, the dashboard bind
address and the licence settings. It is guarded by an allowlist of four keys,
and `anthropic_api_key` is deliberately **not** one of them: the VPS may hold
its own separately-provisioned Claude account, and this sync was only ever
asked for to get a DeepSeek key across.

An allowlist is only an allowlist while something checks it. If it were dropped
or widened, a peer — or anything that could get a message onto the sync
channel — could rewrite arbitrary configuration on the other node, and nothing
in the suite would have noticed.

Nothing here writes to a real database or a real config file.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import _server_peer_data as spd

pytestmark = pytest.mark.asyncio


class _Node(spd.ServerPeerDataMixin):
    """`_handle_ai_config_sync` also refreshes the running engine's cached
    config dict, which `save_to_yaml` cannot reach on its own."""

    def __init__(self):
        self._main_engine = None


@pytest.fixture
def node():
    return _Node()


@pytest.fixture
def saved_yaml(monkeypatch):
    """Capture what would have been written to config.yaml."""
    import backend.src.config as cfg_module
    written: list = []
    monkeypatch.setattr(cfg_module, "save_to_yaml", lambda fields: written.append(fields))
    return written


@pytest.fixture
def db(monkeypatch):
    """Capture every database mutator the handlers can call."""
    calls: list = []
    for name in ("save_synced_learned_rule", "save_ai_recovered_signal",
                 "save_ai_recovered_sl_adjustment",
                 "mark_ai_recovered_signal_approved_by_tg_id",
                 "mark_ai_recovered_signal_rule_result_by_tg_id",
                 "discard_ai_recovered_signal_by_tg_id"):
        def _mk(n):
            def _rec(*a, **kw):
                calls.append((n, a, kw))
            return _rec
        monkeypatch.setattr(spd.db_module, name, _mk(name))
    return calls


class TestTheAiConfigAllowlistIsTheWholeGuard:
    """This writes to config.yaml. Whatever gets past here lands in the same
    file as the bridge URL and the dashboard bind address."""

    async def test_an_allowed_key_is_applied(self, node, saved_yaml):
        node._handle_ai_config_sync({"updates": {"ai_provider": "deepseek"}})

        assert saved_yaml == [{"ai_provider": "deepseek"}]

    @pytest.mark.parametrize("key", [
        "mt5_bridge_url",          # where orders are sent
        "host",                    # what the dashboard binds to
        "starting_balance",        # what every P&L figure is measured against
        "remote_admin_client_enabled",
        "telegram_api_hash",
        "licence_key",
    ])
    async def test_a_key_outside_the_allowlist_is_dropped(self, node,
                                                          saved_yaml, key):
        node._handle_ai_config_sync({"updates": {key: "peer-supplied-value"}})

        assert saved_yaml == [], (
            f"a peer rewrote {key!r} in config.yaml through the AI config sync"
        )

    async def test_anthropic_api_key_is_excluded_ON_PURPOSE(self, node,
                                                            saved_yaml):
        """Not an oversight — the VPS may have its own Claude account, and
        overwriting it with the Mac's would silently move the billing and could
        break a working node."""
        assert "anthropic_api_key" not in node._AI_CONFIG_SYNC_KEYS

        node._handle_ai_config_sync({"updates": {"anthropic_api_key": "sk-ant-x"}})

        assert saved_yaml == []

    async def test_allowed_keys_survive_alongside_rejected_ones(self, node,
                                                               saved_yaml):
        """The realistic shape: a legitimate update with something extra
        riding along. The good half must apply, the rest must not."""
        node._handle_ai_config_sync({"updates": {
            "deepseek_model": "deepseek-chat",
            "mt5_bridge_url": "http://attacker.example",
        }})

        assert saved_yaml == [{"deepseek_model": "deepseek-chat"}]

    async def test_an_empty_update_writes_nothing(self, node, saved_yaml):
        node._handle_ai_config_sync({"updates": {}})
        node._handle_ai_config_sync({})

        assert saved_yaml == []


class TestALearnedRuleNeedsBothItsKeyFields:
    """A rule with no pattern or no channel would be stored and then match
    nothing, or everything."""

    async def test_a_complete_rule_is_mirrored(self, node, db):
        node._handle_learned_rule_sync({
            "channel_name": "Gold VIP", "pattern": r"BUY (\d+)",
        })

        assert [c[0] for c in db] == ["save_synced_learned_rule"]

    @pytest.mark.parametrize("missing", ["channel_name", "pattern"])
    async def test_a_rule_missing_either_field_is_dropped(self, node, db,
                                                          missing):
        msg = {"channel_name": "Gold VIP", "pattern": r"BUY (\d+)"}
        del msg[missing]

        node._handle_learned_rule_sync(msg)

        assert db == []

    async def test_an_empty_field_counts_as_missing(self, node, db):
        node._handle_learned_rule_sync({"channel_name": "", "pattern": "x"})

        assert db == []


class TestTheAiReviewQueueMirror:

    @pytest.mark.parametrize("action,expected", [
        ("created", "save_ai_recovered_signal"),
        ("approved", "mark_ai_recovered_signal_approved_by_tg_id"),
        ("rule_result", "mark_ai_recovered_signal_rule_result_by_tg_id"),
        ("discarded", "discard_ai_recovered_signal_by_tg_id"),
    ])
    async def test_each_action_reaches_its_own_mutator(self, node, db,
                                                       action, expected):
        node._handle_ai_recovered_signal_sync({
            "action": action, "tg_message_id": 12345,
        })

        assert [c[0] for c in db] == [expected]

    async def test_an_sl_adjustment_takes_a_different_path(self, node, db):
        node._handle_ai_recovered_signal_sync({
            "action": "created", "tg_message_id": 12345,
            "message_type": "sl_adjustment", "new_stop_loss": 2390.0,
        })

        assert [c[0] for c in db] == ["save_ai_recovered_sl_adjustment"]

    @pytest.mark.parametrize("missing", ["action", "tg_message_id"])
    async def test_a_message_missing_either_key_field_does_nothing(self, node,
                                                                   db, missing):
        msg = {"action": "approved", "tg_message_id": 12345}
        del msg[missing]

        node._handle_ai_recovered_signal_sync(msg)

        assert db == []

    async def test_an_UNKNOWN_action_does_nothing(self, node, db):
        """No fall-through. A router that defaulted to `created` would forge
        review-queue rows out of malformed messages."""
        node._handle_ai_recovered_signal_sync({
            "action": "wipe_everything", "tg_message_id": 12345,
        })

        assert db == []
