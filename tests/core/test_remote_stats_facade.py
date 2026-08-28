"""Which node's engine data the panels show, Local or mirrored-Remote.

The facade swaps between this machine's own engine data and the VPS's mirrored
snapshot so the panel code needs no branches. The switching is two booleans,
and both encode a distinction that is easy to get backwards.

The one worth reading twice is the Mac/VPS asymmetry. Under centralized signal
generation BOTH machines see `centralized_signal_gen_enabled` true and
`active_trader == remote_vps` -- those are shared settings. What separates them
is that the VPS is running a SyncServer. The VPS's own engines were
centralized OUT and have stopped analysing, so labelling it "Local" would show
permanently-idle data as though it were current.

Nothing here touches a network or a database; the settings, the sync client and
the sync server are all faked.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import remote_stats_facade as facade


class _Client:
    def __init__(self, conn_state="connected", active_trader="remote_vps", stats=None):
        self.conn_state = conn_state
        self.remote_status = {"active_trader": active_trader}
        self.remote_signal_gen_stats = stats if stats is not None else {"breakout": {}}


@pytest.fixture
def wiring(monkeypatch):
    """Baseline: a Mac (no SyncServer), connected, VPS trading, stats present,
    centralized generation OFF."""
    from backend.src.db import database as db_module
    from backend.src.services.cluster.sync import client as sync_client
    from backend.src.services.cluster.sync import server as sync_server

    state = {"rs": {"centralized_signal_gen_enabled": 0},
             "active_trader": "remote_vps",
             "client": _Client(),
             "server": None}

    monkeypatch.setattr(db_module, "get_risk_settings", lambda: state["rs"])
    monkeypatch.setattr(db_module, "get_active_trader", lambda: state["active_trader"])
    monkeypatch.setattr(sync_client, "get_instance", lambda: state["client"])
    monkeypatch.setattr(sync_server, "get_instance", lambda: state["server"])
    return state


class TestTheMacVpsAsymmetry:
    """Both machines read the same shared settings. Only the SyncServer tells
    them apart."""

    def test_the_mac_is_in_centralized_remote_mode(self, wiring):
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        wiring["server"] = None                      # no SyncServer -> this is the Mac
        assert facade._is_centralized_remote_mode() is True

    def test_the_vps_is_not_even_though_the_settings_match(self, wiring):
        """The trap. Identical settings; the VPS must still answer False, or
        its own centralized-out engines get shown as the live local ones."""
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        wiring["server"] = object()                  # a SyncServer -> this is the VPS
        assert facade._is_centralized_remote_mode() is False

    def test_centralized_off_means_neither_is(self, wiring):
        wiring["rs"] = {"centralized_signal_gen_enabled": 0}
        assert facade._is_centralized_remote_mode() is False

    def test_a_local_active_trader_is_not_centralized_remote(self, wiring):
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        wiring["active_trader"] = "local"
        assert facade._is_centralized_remote_mode() is False


class TestWhenRemoteDataIsShown:
    def test_connected_with_vps_trading_and_stats_shows_remote(self, wiring):
        assert facade._is_remote_active() is True

    def test_centralized_generation_forces_LOCAL(self, wiring):
        """Generation has moved to this node, so its own engines are what is
        current. The VPS's have stopped analysing entirely -- showing them
        would be showing permanently-idle data."""
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        assert facade._is_remote_active() is False

    def test_a_disconnected_client_shows_local(self, wiring):
        wiring["client"] = _Client(conn_state="disconnected")
        assert facade._is_remote_active() is False

    def test_a_local_active_trader_shows_local(self, wiring):
        wiring["client"] = _Client(active_trader="local")
        assert facade._is_remote_active() is False

    def test_no_mirrored_stats_yet_shows_local(self, wiring):
        """Just-connected, nothing mirrored across. Empty remote data would
        render every panel as zeros, which reads as "the engine did nothing"
        rather than "no data yet"."""
        wiring["client"] = _Client(stats={})
        assert facade._is_remote_active() is False

    def test_no_client_at_all_shows_local(self, wiring, monkeypatch):
        from backend.src.services.cluster.sync import client as sync_client
        monkeypatch.setattr(sync_client, "get_instance", lambda: None)
        assert facade._is_remote_active() is False

    def test_a_failing_client_lookup_shows_local(self, wiring, monkeypatch):
        """Fails safe to this node's own data. The alternative is a panel that
        errors instead of rendering."""
        from backend.src.services.cluster.sync import client as sync_client
        def _boom():
            raise RuntimeError("sync client unavailable")
        monkeypatch.setattr(sync_client, "get_instance", _boom)
        assert facade._is_remote_active() is False

    def test_unreadable_settings_fall_through_to_the_client_check(self, wiring, monkeypatch):
        """Documenting real behaviour, not endorsing it.

        _is_centralized_remote_mode() swallows its own exceptions and answers
        False, so unreadable settings do NOT stop _is_remote_active() -- it
        carries on and can still answer True from the client alone.

        Benign almost always. The exception is a Mac with centralized
        generation ON at the moment settings become unreadable: the
        centralized check silently answers "not centralized", and the panels
        then show the VPS's engines, which under centralization have stopped
        analysing. Idle data displayed as current.

        Pinned so the behaviour is visible and deliberate rather than
        discovered later. Narrow enough not to be worth changing blind; a
        change here is a money-path panel and wants its own decision."""
        from backend.src.db import database as db_module
        def _boom():
            raise RuntimeError("settings unavailable")
        monkeypatch.setattr(db_module, "get_risk_settings", _boom)
        assert facade._is_centralized_remote_mode() is False
        assert facade._is_remote_active() is True


class TestTheFacadeRoutes:
    class _Real:
        def get_virtual_balance(self): return 1234.5
        def get_max_drawdown(self): return 99.0
        def get_stats(self): return {"source": "local"}
        def get_open_signals(self): return ["local-sig"]

    def test_local_mode_calls_the_real_module(self, wiring):
        wiring["client"] = _Client(conn_state="disconnected")
        db = facade._DbFacade("breakout", self._Real())
        assert db.get_virtual_balance() == 1234.5
        assert db.get_stats() == {"source": "local"}
        assert db.get_open_signals() == ["local-sig"]

    def test_remote_mode_reads_the_mirrored_snapshot(self, wiring):
        wiring["client"] = _Client(stats={"breakout": {
            "virtual_balance": 777.0, "stats": {"source": "vps"},
            "open_signals": ["vps-sig"]}})
        db = facade._DbFacade("breakout", self._Real())
        assert db.get_virtual_balance() == 777.0
        assert db.get_stats() == {"source": "vps"}
        assert db.get_open_signals() == ["vps-sig"]

    def test_each_engine_reads_its_own_key(self, wiring):
        """One shared snapshot holds every engine. Reading the wrong key shows
        the breakout engine's numbers on the reversal panel."""
        wiring["client"] = _Client(stats={
            "breakout": {"virtual_balance": 111.0},
            "reversal": {"virtual_balance": 222.0}})
        assert facade._DbFacade("breakout", self._Real()).get_virtual_balance() == 111.0
        assert facade._DbFacade("reversal", self._Real()).get_virtual_balance() == 222.0

    def test_a_missing_engine_key_yields_a_default_not_an_error(self, wiring):
        wiring["client"] = _Client(stats={"breakout": {}})
        db = facade._DbFacade("nosuch", self._Real())
        assert db.get_virtual_balance() == 0.0
        assert db.get_open_signals() == []
