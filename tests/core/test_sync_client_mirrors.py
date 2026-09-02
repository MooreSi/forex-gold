"""What the Mac copies from the VPS, and what it refuses to copy over.

Four `_mirror_*_locally` methods apply the VPS's confirmed state to this node
so Local mode always starts from an identical baseline. Each carries the same
guard, and the comment on the first says why it exists: this fires on every
reconnect, *including right after the VPS restarts with its old, pre-change
values*, so without the guard it overwrites a just-made local edit with the
VPS's stale one. A real, observed failure mode.

That is the same shape as the question that took a database query and four
greps to answer on 2026-09-02 — settings changing with nothing recording why.
Here the guard is the answer, and it was untested.

Two other properties matter as much:

  * **`_from_sync=True` on every write.** Without it, applying an incoming
    value re-forwards it straight back out and the two nodes echo one change
    at each other indefinitely.
  * **Nothing raises.** These run inside the receive loop, and
    `SyncClient._run_loop` catches everything and reconnects — so an exception
    here does not cost one setting, it drops the link.

No database and no socket: every writer is captured.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import client as sc


@pytest.fixture
def node():
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli._pending_settings = {}
    cli._pending_channel_strategy = {}
    cli._pending_trading_schedule = None
    cli._pending_strategy_params = None
    cli.remote_status = {}
    return cli


@pytest.fixture
def writes(monkeypatch):
    """Capture every downstream write these mirrors can make."""
    seen: dict = {"settings": [], "channel": [], "schedule": [], "params": []}
    monkeypatch.setattr(sc.db_module, "update_risk_settings",
                        lambda updates, **kw: seen["settings"].append((updates, kw)))
    monkeypatch.setattr(sc.db_module, "set_channel_strategy_override",
                        lambda src, strat, auto, **kw: seen["channel"].append((src, strat, auto, kw)))
    monkeypatch.setattr(
        "backend.src.services.risk.schedule.apply_trading_schedule_snapshot",
        lambda snap: seen["schedule"].append(snap))
    monkeypatch.setattr(
        "backend.src.services.risk.strategy_params.apply_strategy_params_snapshot",
        lambda snap: seen["params"].append(snap))
    return seen


class TestMirroringSettings:
    def test_it_applies_what_the_vps_confirmed(self, node, writes):
        node._mirror_settings_locally({"max_daily_loss_pct": 3.0})

        assert writes["settings"][0][0] == {"max_daily_loss_pct": 3.0}

    def test_it_marks_the_write_as_coming_from_sync(self, node, writes):
        """Without this the applied value is forwarded straight back and the
        two nodes echo one change at each other for ever."""
        node._mirror_settings_locally({"max_daily_loss_pct": 3.0})

        assert writes["settings"][0][1].get("_from_sync") is True

    def test_a_pending_local_edit_is_not_overwritten(self, node, writes):
        """The failure this guard exists for: the VPS restarts with its old
        values, reconnects, and its snapshot lands on top of a change the
        user made here seconds ago."""
        node._pending_settings = {"max_daily_loss_pct": 3.0}

        node._mirror_settings_locally({"max_daily_loss_pct": 20.0,
                                       "max_open_trades": 5})

        assert writes["settings"][0][0] == {"max_open_trades": 5}

    def test_nothing_is_written_when_every_key_is_pending(self, node, writes):
        node._pending_settings = {"max_daily_loss_pct": 3.0}

        node._mirror_settings_locally({"max_daily_loss_pct": 20.0})

        assert writes["settings"] == []

    def test_an_empty_snapshot_writes_nothing(self, node, writes):
        node._mirror_settings_locally({})

        assert writes["settings"] == []

    def test_a_database_failure_does_not_escape(self, node, monkeypatch):
        """This runs inside the receive loop. Raising drops the link, so one
        bad setting would cost the whole connection."""
        def _boom(updates, **kw):
            raise RuntimeError("locked")
        monkeypatch.setattr(sc.db_module, "update_risk_settings", _boom)

        node._mirror_settings_locally({"max_daily_loss_pct": 3.0})


class TestMirroringChannelStrategy:
    def test_each_channel_is_applied(self, node, writes):
        node._mirror_channel_strategy_locally(
            {"GD": {"strategy": "scale_out", "auto": True}})

        src, strat, auto, kw = writes["channel"][0]
        assert (src, strat, auto) == ("GD", "scale_out", True)
        assert kw.get("_from_sync") is True

    def test_a_pending_channel_is_skipped(self, node, writes):
        node._pending_channel_strategy = {"GD": {"strategy": "trail_stop"}}

        node._mirror_channel_strategy_locally(
            {"GD": {"strategy": "scale_out"}, "VIP": {"strategy": "be_runner"}})

        assert [w[0] for w in writes["channel"]] == ["VIP"]

    def test_one_bad_channel_does_not_stop_the_others(self, node, writes,
                                                       monkeypatch):
        """Per channel, because a snapshot carries every channel at once and
        one unknown name must not cost the rest."""
        def _set(src, strat, auto, **kw):
            if src == "BAD":
                raise RuntimeError("no such channel")
            writes["channel"].append((src, strat, auto, kw))
        monkeypatch.setattr(sc.db_module, "set_channel_strategy_override", _set)

        node._mirror_channel_strategy_locally(
            {"BAD": {"strategy": "x"}, "GOOD": {"strategy": "y"}})

        assert [w[0] for w in writes["channel"]] == ["GOOD"]


class TestMirroringTheScheduleAndParams:
    def test_the_schedule_is_applied(self, node, writes):
        node._mirror_trading_schedule_locally({"mon": []})

        assert writes["schedule"] == [{"mon": []}]

    def test_a_pending_schedule_blocks_the_whole_snapshot(self, node, writes):
        """Unlike settings, this is all-or-nothing: a schedule is one object
        and half of the VPS's over half of yours is neither."""
        node._pending_trading_schedule = {"mon": ["something"]}

        node._mirror_trading_schedule_locally({"mon": []})

        assert writes["schedule"] == []

    def test_the_params_are_applied(self, node, writes):
        node._mirror_strategy_params_locally({"scale_out": {"tp1": 1}})

        assert writes["params"] == [{"scale_out": {"tp1": 1}}]

    def test_pending_params_block_the_whole_snapshot(self, node, writes):
        node._pending_strategy_params = {"scale_out": {}}

        node._mirror_strategy_params_locally({"scale_out": {"tp1": 1}})

        assert writes["params"] == []

    @pytest.mark.parametrize("method", ["_mirror_trading_schedule_locally",
                                        "_mirror_strategy_params_locally"])
    def test_an_empty_snapshot_writes_nothing(self, node, writes, method):
        getattr(node, method)({})

        assert writes["schedule"] == [] and writes["params"] == []


class TestLookingUpARemotePosition:
    def test_it_finds_a_position_the_other_node_opened(self, node):
        """This node has no local row for a trade the VPS opened, so the
        Active Trade card is rendered from the heartbeat."""
        node.remote_status = {"open_positions": [{"mt5_ticket": 111, "strategy": "x"}]}

        assert node.get_remote_open_position(111)["strategy"] == "x"

    def test_a_string_ticket_still_matches(self, node):
        """Tickets arrive as strings from some callers and ints from others."""
        node.remote_status = {"open_positions": [{"mt5_ticket": 111}]}

        assert node.get_remote_open_position("111") is not None

    def test_an_unknown_ticket_is_none(self, node):
        node.remote_status = {"open_positions": [{"mt5_ticket": 111}]}

        assert node.get_remote_open_position(222) is None

    @pytest.mark.parametrize("bad", [None, 0, "", "not-a-ticket"])
    def test_junk_is_none_rather_than_an_error(self, node, bad):
        node.remote_status = {"open_positions": [{"mt5_ticket": 111}]}

        assert node.get_remote_open_position(bad) is None

    def test_no_heartbeat_yet_is_none(self, node):
        assert node.get_remote_open_position(111) is None
