"""Data the OTHER node sends, written into this node's database.

These handlers take a websocket message and write it to disk. That is a
different risk shape from the rest of the sync client: the payload is not
something this node produced, so every one of them guards on the fields it
cannot do without and returns quietly rather than writing a half-row.

Two mirrors are covered:

  * Learned parser rules. A rule approved on the VPS is copied here so this
    node's independent Telethon session parses the same message shape
    deterministically, instead of paying for its own AI fallback and asking for
    the same approval again.
  * The AI-recovered signal review queue. Four event types (created, approved,
    rule_result, discarded) plus a periodic full-snapshot backfill for anything
    created while this node was disconnected.

Every db_module call is recorded rather than run, so what is asserted is
exactly which write the handler chose and with what -- which is where these go
wrong.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync.client import SyncClient


pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture
def writes(monkeypatch):
    """Records every mirror write instead of performing it."""
    from backend.src.db import database as db_module
    seen: list[tuple] = []
    for name in ("save_synced_learned_rule",
                 "save_ai_recovered_signal",
                 "save_ai_recovered_sl_adjustment",
                 "mark_ai_recovered_signal_approved_by_tg_id",
                 "mark_ai_recovered_signal_rule_result_by_tg_id",
                 "discard_ai_recovered_signal_by_tg_id"):
        monkeypatch.setattr(db_module, name,
                            (lambda n: lambda *a, **kw: seen.append((n, a, kw)))(name))
    return seen


@pytest.fixture
def client():
    return SyncClient()


class TestMirroredLearnedRules:
    def test_a_complete_rule_is_written(self, client, writes):
        client._handle_learned_rule_sync({
            "channel_name": "Gold Diggers VIP", "pattern": r"BUY (\d+)",
            "rule_type": "ai_derived_parser", "action": "auto_parse",
            "notes": "from VPS", "source_msg_id": "123"})

        assert len(writes) == 1
        name, args, _ = writes[0]
        assert name == "save_synced_learned_rule"
        assert args[0] == "Gold Diggers VIP"
        assert args[2] == r"BUY (\d+)"

    @pytest.mark.parametrize("missing", ["channel_name", "pattern"])
    def test_a_rule_missing_an_ESSENTIAL_field_is_dropped(self, client, writes, missing):
        """A rule with no pattern matches nothing; one with no channel would
        be applied to every channel. Neither is a row worth writing."""
        msg = {"channel_name": "Gold Diggers VIP", "pattern": "BUY"}
        del msg[missing]

        client._handle_learned_rule_sync(msg)

        assert writes == []

    def test_an_EMPTY_pattern_is_dropped_too(self, client, writes):
        """Not just absent. An empty pattern is a rule that matches
        everything, which would hand every message to one parser."""
        client._handle_learned_rule_sync(
            {"channel_name": "Gold Diggers VIP", "pattern": ""})

        assert writes == []

    def test_the_optional_fields_have_sensible_defaults(self, client, writes):
        """An older peer may not send them. Defaulting to the ordinary parser
        rule type is what keeps a mixed-version pair working."""
        client._handle_learned_rule_sync(
            {"channel_name": "GD", "pattern": "BUY"})

        _, args, _ = writes[0]
        assert args[1] == "ai_derived_parser"
        assert args[3] == "auto_parse"


class TestMirroredAiRecoveredEvents:
    def _msg(self, **over):
        m = {"action": "created", "tg_message_id": "19886",
             "channel_name": "Gold Diggers VIP", "raw_text": "BUY NOW",
             "direction": "BUY", "entry_low": 4500.0, "entry_high": 4502.0,
             "stop_loss": 4495.0, "tp1": 4505.0, "confidence": 0.82,
             "reasoning": "matched the usual shape"}
        m.update(over)
        return m

    def test_a_created_signal_is_saved(self, client, writes):
        client._handle_ai_recovered_signal_sync(self._msg())

        name, args, _ = writes[0]
        assert name == "save_ai_recovered_signal"
        assert args[0] == "19886"
        assert args[3]["direction"] == "BUY"
        assert args[3]["stop_loss"] == 4495.0

    def test_ALL_EIGHT_take_profits_are_carried(self, client, writes):
        """The parsed dict is built from a fixed key list. A short list would
        silently drop the later targets from a mirrored signal."""
        msg = self._msg(**{f"tp{i}": 4500.0 + i for i in range(1, 9)})

        client._handle_ai_recovered_signal_sync(msg)

        parsed = writes[0][1][3]
        for i in range(1, 9):
            assert parsed[f"tp{i}"] == 4500.0 + i, f"tp{i} was dropped"

    def test_an_sl_adjustment_takes_a_DIFFERENT_write(self, client, writes):
        """A follow-up to an existing trade, not a new signal. Routing it to
        the signal table would create a phantom entry with no levels."""
        client._handle_ai_recovered_signal_sync(self._msg(
            message_type="sl_adjustment", new_stop_loss=4490.0))

        assert writes[0][0] == "save_ai_recovered_sl_adjustment"
        assert writes[0][1][3] == 4490.0

    @pytest.mark.parametrize("action,expected", [
        ("approved", "mark_ai_recovered_signal_approved_by_tg_id"),
        ("rule_result", "mark_ai_recovered_signal_rule_result_by_tg_id"),
        ("discarded", "discard_ai_recovered_signal_by_tg_id"),
    ])
    def test_each_action_takes_its_own_path(self, client, writes, action, expected):
        """A crossed mapping would discard a signal the operator approved."""
        client._handle_ai_recovered_signal_sync(
            self._msg(action=action, rule_generated=True))

        assert [w[0] for w in writes] == [expected]

    def test_rule_result_carries_the_flag_as_a_bool(self, client, writes):
        """It arrives as JSON and reaches a column that is read as a boolean.
        A truthy string would make every result look successful."""
        client._handle_ai_recovered_signal_sync(
            self._msg(action="rule_result", rule_generated=0, rule_gen_note="no match"))

        _, args, _ = writes[0]
        assert args[1] is False
        assert args[2] == "no match"

    @pytest.mark.parametrize("missing", ["action", "tg_message_id"])
    def test_a_message_missing_an_essential_field_is_dropped(self, client, writes, missing):
        msg = self._msg()
        del msg[missing]

        client._handle_ai_recovered_signal_sync(msg)

        assert writes == []

    def test_an_UNKNOWN_action_writes_NOTHING(self, client, writes):
        """A newer peer sending an action this build does not know must not
        fall through into one of the existing branches."""
        client._handle_ai_recovered_signal_sync(self._msg(action="unapproved_by_a_cat"))

        assert writes == []


class TestTheFullSnapshotBackfill:
    """Periodic resync. This is what recovers anything created while this node
    was disconnected, or before the feature existed."""

    def _row(self, **over):
        r = {"tg_message_id": "19886", "channel_name": "GD", "raw_text": "BUY",
             "direction": "BUY", "entry_low": 4500.0, "entry_high": 4502.0,
             "stop_loss": 4495.0, "confidence": 0.5, "reasoning": "x"}
        r.update(over)
        return r

    def test_a_row_is_applied_like_a_created_event(self, client, writes):
        client._apply_ai_recovered_snapshot_row(self._row())

        assert writes[0][0] == "save_ai_recovered_signal"
        assert writes[0][1][0] == "19886"

    def test_an_sl_adjustment_row_routes_the_same_way_as_a_live_one(self, client, writes):
        """The snapshot and the live path must agree. If only one of them
        knows about sl_adjustment rows, a reconnect rewrites them as signals."""
        client._apply_ai_recovered_snapshot_row(self._row(
            message_type="sl_adjustment", new_stop_loss=4490.0))

        assert writes[0][0] == "save_ai_recovered_sl_adjustment"

    def test_a_row_with_no_id_is_skipped(self, client, writes):
        """Snapshots come in bulk. One bad row must not stop the rest, and it
        must not be written under a null key."""
        row = self._row()
        del row["tg_message_id"]

        client._apply_ai_recovered_snapshot_row(row)

        assert writes == []

    def test_a_bad_row_does_not_stop_the_good_ones(self, client, writes):
        bad = self._row()
        del bad["tg_message_id"]

        client._apply_ai_recovered_snapshot_row(bad)
        client._apply_ai_recovered_snapshot_row(self._row(tg_message_id="19887"))

        assert [w[1][0] for w in writes] == ["19887"]
