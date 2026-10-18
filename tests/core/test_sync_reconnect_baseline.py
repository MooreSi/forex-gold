"""On every (re)connect the Mac's settings win, not the VPS's.

Seen 2026-10-13: the Mac had been the active node, the VPS was then
reconnected, and the VPS did not take the Mac's settings. `_connect_once`
mirrored the VPS's welcome snapshot DOWN over the Mac's own database, and the
only protection was the `_pending_*` queue, which holds only a change that went
through a `propose_*` call and has not been confirmed. Anything that reached
the Mac's database another way, or any change whose queue was lost, was
overwritten by the VPS's older value, silently, and the two nodes then agreed
on the wrong thing.

The rule (owner, 2026-10-14): the local node controls the settings. So on
connect the Mac compares its own state with the welcome snapshot, queues every
difference as a pending proposal, and only then mirrors down, which skips what
is queued. Two things stay VPS-owned because only the VPS can know them: the
circuit breaker's runtime state, and the day the daily goal was resumed.

No socket, no database: every read and writer is captured.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import client as sc


pytestmark = pytest.mark.asyncio


@pytest.fixture
def node(monkeypatch):
    cli = sc.SyncClient.__new__(sc.SyncClient)
    cli._pending_settings = {}
    cli._pending_channel_strategy = {}
    cli._pending_trading_schedule = None
    cli._pending_strategy_params = None
    cli._pending_expert_params = None
    cli.remote_status = {}
    cli._ws = None
    cli.conn_state = "disconnected"
    for persist in ("_persist_pending", "_persist_pending_channel_strategy",
                    "_persist_pending_trading_schedule",
                    "_persist_pending_strategy_params",
                    "_persist_pending_expert_params"):
        monkeypatch.setattr(cli, persist, lambda: None)
    return cli


@pytest.fixture
def local(monkeypatch):
    """This Mac's own state. Tests overwrite the entries they care about."""
    state = {
        "settings": {}, "channel": {}, "schedule": {}, "params": {}, "expert": {},
    }
    monkeypatch.setattr(sc.db_module, "get_risk_settings", lambda: dict(state["settings"]))
    monkeypatch.setattr(sc.db_module, "get_all_channel_strategy_overrides",
                        lambda: dict(state["channel"]))
    monkeypatch.setattr(
        "backend.src.services.risk.schedule.trading_schedule_snapshot",
        lambda: dict(state["schedule"]))
    monkeypatch.setattr(
        "backend.src.services.risk.strategy_params.strategy_params_snapshot",
        lambda: dict(state["params"]))
    monkeypatch.setattr(
        "backend.src.services.risk.expert_params.snapshot",
        lambda: dict(state["expert"]))
    return state


@pytest.fixture
def writes(monkeypatch):
    """Every downstream write the mirror can make."""
    seen: dict = {"settings": [], "channel": [], "schedule": [], "params": [], "expert": []}
    monkeypatch.setattr(sc.db_module, "update_risk_settings",
                        lambda updates, **kw: seen["settings"].append(updates))
    monkeypatch.setattr(sc.db_module, "set_channel_strategy_override",
                        lambda src, strat, auto, **kw: seen["channel"].append(src))
    monkeypatch.setattr(
        "backend.src.services.risk.schedule.apply_trading_schedule_snapshot",
        lambda snap: seen["schedule"].append(snap))
    monkeypatch.setattr(
        "backend.src.services.risk.strategy_params.apply_strategy_params_snapshot",
        lambda snap: seen["params"].append(snap))
    monkeypatch.setattr(
        "backend.src.services.risk.expert_params.apply_snapshot",
        lambda snap: seen["expert"].append(snap))
    return seen


def _welcome(**parts):
    return {"settings": {}, "channel_strategy": {}, "trading_schedule": {},
            "strategy_params": {}, "expert_params": {}, **parts}


class TestSettings:
    async def test_a_value_the_vps_holds_differently_is_queued_for_it(self, node, local, writes):
        local["settings"] = {"max_daily_loss_pct": 8.0, "max_open_trades": 3}

        node._adopt_peer_state(_welcome(settings={"max_daily_loss_pct": 20.0,
                                                  "max_open_trades": 3}))

        assert node._pending_settings == {"max_daily_loss_pct": 8.0}

    async def test_the_vps_value_is_not_written_over_the_queued_one(self, node, local, writes):
        """The bug itself: the welcome snapshot used to land on top of the Mac's
        own value."""
        local["settings"] = {"max_daily_loss_pct": 8.0}

        node._adopt_peer_state(_welcome(settings={"max_daily_loss_pct": 20.0}))

        assert all("max_daily_loss_pct" not in w for w in writes["settings"])

    async def test_a_key_the_vps_has_never_seen_is_sent(self, node, local, writes):
        local["settings"] = {"giveback_guard_enabled": 1}

        node._adopt_peer_state(_welcome(settings={}))

        assert node._pending_settings == {"giveback_guard_enabled": 1}

    async def test_one_stored_as_true_and_one_as_1_are_the_same_value(self, node, local, writes):
        local["settings"] = {"dpm_enabled": True}

        node._adopt_peer_state(_welcome(settings={"dpm_enabled": 1}))

        assert node._pending_settings == {}

    async def test_a_setting_that_is_not_synced_is_never_sent(self, node, local, writes):
        local["settings"] = {"trading_clock_offset_min": 60, "not_a_setting": 1}

        node._adopt_peer_state(_welcome(settings={"trading_clock_offset_min": 0}))

        assert node._pending_settings == {}

    async def test_the_circuit_breaker_state_stays_the_vps_s(self, node, local, writes):
        """Only the VPS knows it tripped. Sending the Mac's idle copy back would
        reset a breaker that is holding trading off."""
        local["settings"] = {"circuit_breaker_active_until": 0.0,
                             "circuit_breaker_consec_losses": 0,
                             "circuit_breaker_losses": 3}

        node._adopt_peer_state(_welcome(settings={"circuit_breaker_active_until": 1.8e9,
                                                  "circuit_breaker_consec_losses": 3,
                                                  "circuit_breaker_losses": 3}))

        assert node._pending_settings == {}
        assert writes["settings"][0]["circuit_breaker_active_until"] == 1.8e9

    async def test_what_already_agrees_is_still_mirrored(self, node, local, writes):
        local["settings"] = {"max_open_trades": 3}

        node._adopt_peer_state(_welcome(settings={"max_open_trades": 3,
                                                  "circuit_breaker_consec_losses": 2}))

        assert node._pending_settings == {}
        assert writes["settings"][0] == {"max_open_trades": 3,
                                         "circuit_breaker_consec_losses": 2}

    async def test_the_queued_change_is_persisted(self, node, local, writes, monkeypatch):
        saved = []
        monkeypatch.setattr(node, "_persist_pending", lambda: saved.append(dict(node._pending_settings)))
        local["settings"] = {"max_open_trades": 5}

        node._adopt_peer_state(_welcome(settings={"max_open_trades": 3}))

        assert saved == [{"max_open_trades": 5}]


class TestChannelStrategy:
    async def test_an_override_the_vps_lacks_or_holds_differently_is_queued(self, node, local, writes):
        local["channel"] = {"A": {"strategy": "grid", "auto": True},
                            "B": {"strategy": "scale_out", "auto": False},
                            "C": {"strategy": "x", "auto": True}}

        node._adopt_peer_state(_welcome(channel_strategy={
            "A": {"strategy": "conservative", "auto": True},
            "C": {"strategy": "x", "auto": True}}))

        assert node._pending_channel_strategy == {
            "A": {"strategy": "grid", "auto": True},
            "B": {"strategy": "scale_out", "auto": False}}
        assert writes["channel"] == ["C"]

    async def test_a_channel_only_the_vps_has_is_left_alone(self, node, local, writes):
        local["channel"] = {}

        node._adopt_peer_state(_welcome(channel_strategy={
            "VPS only": {"strategy": "x", "auto": True}}))

        assert node._pending_channel_strategy == {}


class TestTradingSchedule:
    async def test_a_different_schedule_is_queued_whole(self, node, local, writes):
        local["schedule"] = {"enabled": True, "schedule": {"london": [8, 16]},
                             "daily_target": 30.0}

        node._adopt_peer_state(_welcome(trading_schedule={
            "enabled": False, "schedule": {"london": [7, 17]}, "daily_target": 30.0}))

        assert node._pending_trading_schedule == local["schedule"]
        assert writes["schedule"] == []

    async def test_the_resume_marker_is_never_sent_or_compared(self, node, local, writes):
        """`daily_target_resumed_day` is set on the node that resumed. Sent as
        '' from a Mac that did not, it re-arms a goal the operator resumed past
        (apply_trading_schedule_snapshot: absent, not empty, means 'leave it')."""
        local["schedule"] = {"enabled": True, "daily_target": 30.0,
                             "daily_target_resumed_day": ""}

        node._adopt_peer_state(_welcome(trading_schedule={
            "enabled": True, "daily_target": 30.0,
            "daily_target_resumed_day": "2026-10-13"}))

        assert node._pending_trading_schedule is None

        local["schedule"] = {"enabled": True, "daily_target": 10.0,
                             "daily_target_resumed_day": ""}
        node._adopt_peer_state(_welcome(trading_schedule={
            "enabled": True, "daily_target": 30.0,
            "daily_target_resumed_day": "2026-10-13"}))

        assert "daily_target_resumed_day" not in node._pending_trading_schedule
        assert node._pending_trading_schedule["daily_target"] == 10.0

    async def test_an_empty_local_schedule_does_not_wipe_the_vps(self, node, local, writes):
        local["schedule"] = {}

        node._adopt_peer_state(_welcome(trading_schedule={"enabled": True}))

        assert node._pending_trading_schedule is None

    async def test_a_change_already_queued_is_kept(self, node, local, writes):
        node._pending_trading_schedule = {"enabled": True, "daily_target": 99.0}
        local["schedule"] = {"enabled": True, "daily_target": 10.0}

        node._adopt_peer_state(_welcome(trading_schedule={"enabled": True, "daily_target": 30.0}))

        assert node._pending_trading_schedule == {"enabled": True, "daily_target": 99.0}


class TestStrategyAndExpertParams:
    async def test_different_strategy_params_are_queued(self, node, local, writes):
        local["params"] = {"grid": {"step": 4}}

        node._adopt_peer_state(_welcome(strategy_params={"grid": {"step": 9}}))

        assert node._pending_strategy_params == {"grid": {"step": 4}}
        assert writes["params"] == []

    async def test_equal_strategy_params_queue_nothing(self, node, local, writes):
        local["params"] = {"grid": {"step": 4}}

        node._adopt_peer_state(_welcome(strategy_params={"grid": {"step": 4}}))

        assert node._pending_strategy_params is None

    async def test_different_expert_params_are_queued_and_not_overwritten(self, node, local, writes):
        local["expert"] = {"min_tp1_rr": 0.2, "auto_regime_min_dwell_ticks": 1}

        node._adopt_peer_state(_welcome(expert_params={"min_tp1_rr": 0.2}))

        assert node._pending_expert_params == local["expert"]
        assert writes["expert"] == []

    async def test_empty_local_params_do_not_wipe_the_vps(self, node, local, writes):
        node._adopt_peer_state(_welcome(strategy_params={"grid": {"step": 9}},
                                        expert_params={"min_tp1_rr": 0.2}))

        assert node._pending_strategy_params is None
        assert node._pending_expert_params is None


class TestNothingRaises:
    """This runs inside the receive loop; an exception drops the link and the
    reconnect hits the same exception, for ever."""

    async def test_an_unreadable_local_database_still_adopts_the_vps(self, node, writes, monkeypatch):
        def boom(*a, **k):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(sc.db_module, "get_risk_settings", boom)
        monkeypatch.setattr(sc.db_module, "get_all_channel_strategy_overrides", boom)
        monkeypatch.setattr("backend.src.services.risk.schedule.trading_schedule_snapshot", boom)
        monkeypatch.setattr("backend.src.services.risk.strategy_params.strategy_params_snapshot", boom)
        monkeypatch.setattr("backend.src.services.risk.expert_params.snapshot", boom)

        node._adopt_peer_state(_welcome(settings={"max_open_trades": 3}))

        assert node._pending_settings == {}
        assert writes["settings"] == [{"max_open_trades": 3}]

    async def test_the_remote_snapshots_are_kept_for_the_dashboard(self, node, local, writes):
        node._adopt_peer_state(_welcome(settings={"max_open_trades": 3},
                                        channel_strategy={"A": {"strategy": "x", "auto": True}}))

        assert node.remote_settings == {"max_open_trades": 3}
        assert node.remote_channel_strategy == {"A": {"strategy": "x", "auto": True}}
