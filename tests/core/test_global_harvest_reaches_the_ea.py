"""A Global Harvest change reaches the running EA, not just the database.

The EA holds Harvest in memory only (`g_globalHarvestEnabled` in
ForexTraderBridge.mq5) and learns it from `push_global_config()`. The NiceGUI
Global Parameters card called that after every save; the React port
(2026-09-18) dropped the call, and the VPS applying a Mac's proposal never made
it at all. So a toggle saved on either node sat in both databases while the EA
went on harvesting (or not) with whatever it was told on its last "hello".
Found 2026-09-29: the owner switched Harvest on at the Mac and asked whether
the VPS was now watching the live trades with it. It was not.

`update_risk_settings` is the one place every such change passes through --
a local save, the VPS applying a Mac's proposal, and the Mac mirroring the
VPS's confirmed snapshot -- so that is where the push is made.

Nothing here reaches a broker: the EA bridge is a MagicMock whose
`push_global_config` is a recording sentinel, and no socket is opened.
"""
from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from backend.src.services.broker import ea_bridge
from backend.src.services.risk import risk_settings_repo


@pytest.fixture
def ea(fresh_db, monkeypatch):
    """A connected EA whose push only records that it was asked, plus a
    scheduler that runs nothing (the sync forward is not under test)."""
    bridge = MagicMock()
    bridge.push_global_config = MagicMock(return_value="pushed")
    monkeypatch.setattr(ea_bridge, "get_instance", lambda: bridge)
    scheduled = []
    monkeypatch.setattr(risk_settings_repo, "_schedule_coro", scheduled.append)
    monkeypatch.setattr(risk_settings_repo, "_forward_settings_over_sync", lambda _u: None)
    bridge.scheduled = scheduled
    return bridge


def _start(value: int) -> None:
    risk_settings_repo.update_risk_settings({"global_harvest_enabled": value}, _from_sync=True)


@pytest.mark.parametrize("from_sync", [False, True], ids=["local save", "applied from sync"])
def test_turning_harvest_on_pushes_it_to_the_ea(ea, from_sync):
    _start(0)
    ea.push_global_config.reset_mock()
    ea.scheduled.clear()

    risk_settings_repo.update_risk_settings({"global_harvest_enabled": 1}, _from_sync=from_sync)

    ea.push_global_config.assert_called_once_with()
    assert ea.scheduled == ["pushed"]


def test_a_new_threshold_is_pushed_too(ea):
    _start(1)
    ea.push_global_config.reset_mock()

    risk_settings_repo.update_risk_settings({"global_harvest_threshold_usd": 75.0})

    ea.push_global_config.assert_called_once_with()


def test_an_unchanged_harvest_value_is_not_pushed_again(ea):
    """The Mac mirrors the VPS's whole snapshot on every broadcast; the EA
    should not be told the same thing each time."""
    _start(1)
    ea.push_global_config.reset_mock()

    risk_settings_repo.update_risk_settings(
        {"global_harvest_enabled": 1, "max_open_trades": 4}, _from_sync=True)

    ea.push_global_config.assert_not_called()


def test_other_settings_do_not_push(ea):
    _start(0)
    ea.push_global_config.reset_mock()

    risk_settings_repo.update_risk_settings({"max_open_trades": 3})

    ea.push_global_config.assert_not_called()


def test_no_ea_connected_is_not_an_error(fresh_db, monkeypatch):
    monkeypatch.setattr(ea_bridge, "get_instance", lambda: None)
    monkeypatch.setattr(risk_settings_repo, "_forward_settings_over_sync", lambda _u: None)
    _start(0)

    result = risk_settings_repo.update_risk_settings({"global_harvest_enabled": 1})

    assert result["global_harvest_enabled"] == 1
