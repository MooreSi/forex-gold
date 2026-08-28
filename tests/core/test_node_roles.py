"""Which node of a paired Mac/VPS install owns which job.

Two mutual-exclusion gates. Both had a real outage behind them, and both fail
OPEN -- an unpaired install has no counterpart, and an error here must not
silently kill trading or bot control. Getting either backwards is expensive:

  * is_bot_command_authority() decides who long-polls the Telegram bot token.
    Only one process may. Two pollers give each other 409 Conflict forever,
    each side's deleteWebhook kicking the other -- the conflict cycle in the
    2026 logs.
  * is_active_trader_node() decides who spends paid AI credits on signal
    recovery. Both nodes parse every message independently, so before this
    gate both called the AI fallback on the same messages -- double cost, and
    a review queue split across two node-local databases with no way to see
    the other node's entries from either UI.

Nothing here opens a socket or touches a database.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import node_roles
from backend.src.services.cluster.sync.protocol import (
    TRADER_LOCAL, TRADER_REMOTE_VPS,
)


class _Server:
    def __init__(self, standing_down: bool):
        self._sd = standing_down

    def is_standing_down(self) -> bool:
        return self._sd


@pytest.fixture
def wiring(monkeypatch):
    """Baseline: a standalone install. No sync server, no configured host."""
    from backend.src.db import database as db_module
    from backend.src.services.cluster.sync import client as sync_client
    from backend.src.services.cluster.sync import server as sync_server

    state = {"server": None, "host": "", "active_trader": TRADER_LOCAL,
             "sync_server_enabled": "0"}

    monkeypatch.setattr(sync_server, "get_instance", lambda: state["server"])
    monkeypatch.setattr(sync_client.SyncClient, "load_config",
                        staticmethod(lambda: (state["host"], 9001, "tok")))
    monkeypatch.setattr(db_module, "get_active_trader",
                        lambda: state["active_trader"])
    monkeypatch.setattr(db_module, "get_app_config",
                        lambda k, *a, **kw: state["sync_server_enabled"]
                        if k == "sync_server_enabled" else None)
    return state


class TestIsActiveTraderNode:
    """Gates paid AI credits. False means 'the other node is trading, let it
    pay'."""

    def test_a_standalone_install_is_always_the_active_trader(self, wiring):
        assert node_roles.is_active_trader_node() is True

    def test_a_standing_down_server_is_not(self, wiring):
        """The VPS has handed control back. It is a view-only dashboard."""
        wiring["server"] = _Server(standing_down=True)
        assert node_roles.is_active_trader_node() is False

    def test_a_server_that_is_not_standing_down_still_is(self, wiring):
        wiring["server"] = _Server(standing_down=False)
        assert node_roles.is_active_trader_node() is True

    def test_a_paired_mac_with_the_vps_trading_is_not(self, wiring):
        """Client side of the same exclusion: a host is configured and the
        switch says the VPS trades, so the Mac must not spend the credits."""
        wiring["host"] = "203.0.113.10"
        wiring["active_trader"] = TRADER_REMOTE_VPS
        assert node_roles.is_active_trader_node() is False

    def test_a_paired_mac_that_is_itself_trading_is(self, wiring):
        wiring["host"] = "203.0.113.10"
        wiring["active_trader"] = TRADER_LOCAL
        assert node_roles.is_active_trader_node() is True

    def test_no_host_configured_beats_the_active_trader_setting(self, wiring):
        """Both conditions are required. A leftover remote_vps setting on a
        machine with no counterpart must not stop it working."""
        wiring["host"] = ""
        wiring["active_trader"] = TRADER_REMOTE_VPS
        assert node_roles.is_active_trader_node() is True

    def test_standing_down_wins_over_the_client_check(self, wiring):
        wiring["server"] = _Server(standing_down=True)
        wiring["host"] = ""
        wiring["active_trader"] = TRADER_LOCAL
        assert node_roles.is_active_trader_node() is False

    def test_a_RAISING_lookup_PROPAGATES_despite_the_docstring(self, wiring,
                                                              monkeypatch):
        """Records a mismatch, does not bless it.

        The docstring says this "fails open (True) on any error". It does not.
        The two try blocks catch ImportError ONLY, so a database error out of
        get_active_trader() propagates to the caller.

        Its one caller wraps it, so today this surfaces as the AI fallback
        being skipped rather than a crash -- which is fail-CLOSED, the
        opposite of what is written. Left as-is because changing an error path
        on a live gate deserves its own change; the sibling function below
        shows what the docstring describes.
        """
        from backend.src.db import database as db_module

        def _boom():
            raise RuntimeError("database is locked")

        wiring["host"] = "203.0.113.10"
        monkeypatch.setattr(db_module, "get_active_trader", _boom)

        with pytest.raises(RuntimeError):
            node_roles.is_active_trader_node()


class TestIsBotCommandAuthority:
    """Gates Telegram getUpdates. Two True answers at once is the 409 loop."""

    def test_a_standalone_install_polls(self, wiring):
        assert node_roles.is_bot_command_authority() is True

    def test_the_vps_polls_only_when_it_is_the_active_trader(self, wiring):
        wiring["sync_server_enabled"] = "1"
        wiring["active_trader"] = TRADER_REMOTE_VPS
        assert node_roles.is_bot_command_authority() is True

    def test_the_vps_stands_down_when_the_mac_trades(self, wiring):
        wiring["sync_server_enabled"] = "1"
        wiring["active_trader"] = TRADER_LOCAL
        assert node_roles.is_bot_command_authority() is False

    def test_the_mac_polls_only_when_it_is_the_active_trader(self, wiring):
        wiring["host"] = "203.0.113.10"
        wiring["active_trader"] = TRADER_LOCAL
        assert node_roles.is_bot_command_authority() is True

    def test_the_mac_stands_down_when_the_vps_trades(self, wiring):
        wiring["host"] = "203.0.113.10"
        wiring["active_trader"] = TRADER_REMOTE_VPS
        assert node_roles.is_bot_command_authority() is False

    @pytest.mark.parametrize("active", [TRADER_LOCAL, TRADER_REMOTE_VPS])
    def test_a_paired_pair_never_both_poll(self, wiring, active):
        """THE property. Whatever the switch says, exactly one side answers
        True -- otherwise both long-poll the same token and 409 each other."""
        wiring["active_trader"] = active

        wiring["sync_server_enabled"] = "1"
        wiring["host"] = ""
        vps = node_roles.is_bot_command_authority()

        wiring["sync_server_enabled"] = "0"
        wiring["host"] = "203.0.113.10"
        mac = node_roles.is_bot_command_authority()

        assert vps != mac, (
            f"both nodes answered {vps} with active_trader={active!r} — "
            "they will fight over the bot token")

    def test_a_failing_lookup_fails_OPEN(self, wiring, monkeypatch):
        """Deliberate. A database blip must not leave the user with no bot
        control at all; a transient 409 is the lesser harm."""
        from backend.src.db import database as db_module

        def _boom(*a, **kw):
            raise RuntimeError("database is locked")

        monkeypatch.setattr(db_module, "get_app_config", _boom)
        assert node_roles.is_bot_command_authority() is True

    def test_sync_server_enabled_is_compared_as_the_STRING_one(self, wiring):
        """app_config stores text. An int 1, or "true", is not this value --
        the VPS would fall through to the client branch, find no host and
        answer True unconditionally, which is the 409 loop again."""
        wiring["sync_server_enabled"] = 1          # int, not "1"
        wiring["host"] = ""
        wiring["active_trader"] = TRADER_LOCAL
        assert node_roles.is_bot_command_authority() is True
