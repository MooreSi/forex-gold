"""The controller operations that are NOT plain forwarders.

`test_controller_forwarding.py` sweeps the 213 operations whose entire
specification is "pass this through unchanged". The 38 that are left are here,
one behaviour at a time, because each of them does something a sweep cannot
assert: a guard, a branch, a lazily-constructed singleton, a re-exported
constant, or a copy made on purpose.

They are also the ones where the logic is small enough to look harmless and
expensive to get wrong. `reader_is_configured` is the worked example: the view
that used to own it tested `status["connected"] or status["authenticated"]`,
and the reader's status dict carries neither key — so both lookups returned
None and the Getting Started row was red on every install however well Telegram
was configured (owner report, 2026-09-02). Moving it into a controller is what
made it testable; this is the test.

**No test in this file can reach a broker, a network or a database.** Every
service singleton is replaced with a stand-in before the controller is called.
"""
from __future__ import annotations

import asyncio
import datetime as dt

import pytest

from backend.src.controllers import auth_controller as auth_ctl
from backend.src.controllers import broker_controller as broker_ctl
from backend.src.controllers import history_controller as history_ctl
from backend.src.controllers import notifications_controller as notifications_ctl
from backend.src.controllers import remote_node_controller as remote_node_ctl
from backend.src.controllers import sync_controller as sync_ctl
from backend.src.controllers import system_controller as system_ctl
from backend.src.controllers import telegram_controller as tg_ctl
from backend.src.controllers import trading_controller as trading_ctl


def _run(awaitable):
    return asyncio.run(awaitable)


class _Recorder:
    """Records what it was called with and returns a marker to check identity."""

    def __init__(self, result=None) -> None:
        self.calls: list[tuple[tuple, dict]] = []
        self.result = result if result is not None else object()

    def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.result


class _AsyncRecorder(_Recorder):
    async def __call__(self, *args, **kwargs):     # type: ignore[override]
        self.calls.append((args, kwargs))
        return self.result


# ── The EA's health ──────────────────────────────────────────────────────────

class TestEaHealth:
    """"Collapses the `instance is None or not instance.is_ea_healthy()` pair
    the pages were writing by hand, so no page needs the instance to ask." The
    two halves are separate tests because they fail differently: no bridge at
    all, versus a bridge whose EA has gone quiet."""

    class _Ea:
        def __init__(self, healthy: bool, last_seen: float = 0.0) -> None:
            self._healthy = healthy
            self._last_seen = last_seen

        def is_ea_healthy(self) -> bool:
            return self._healthy

    def test_no_bridge_at_all_is_not_healthy(self, monkeypatch):
        monkeypatch.setattr(broker_ctl._bridge, "get_instance", lambda: None)

        assert broker_ctl.ea_is_healthy() is False

    def test_a_bridge_whose_ea_has_gone_quiet_is_not_healthy(self, monkeypatch):
        monkeypatch.setattr(broker_ctl._bridge, "get_instance",
                            lambda: self._Ea(healthy=False))

        assert broker_ctl.ea_is_healthy() is False

    def test_a_connected_ea_with_a_current_heartbeat_is_healthy(self, monkeypatch):
        """Negative control for the two above: without this, a function that
        always returned False would pass both."""
        monkeypatch.setattr(broker_ctl._bridge, "get_instance",
                            lambda: self._Ea(healthy=True))

        assert broker_ctl.ea_is_healthy() is True

    def test_seconds_since_last_seen_is_none_when_there_is_no_bridge(self, monkeypatch):
        monkeypatch.setattr(broker_ctl._bridge, "get_instance", lambda: None)

        assert broker_ctl.ea_seconds_since_last_seen() is None

    def test_it_is_none_when_the_bridge_has_never_heard_from_an_ea(self, monkeypatch):
        """"None covers both 'no bridge yet' and 'bridge exists but never heard
        from an EA'." Returning 0 would render as "last seen: just now"."""
        monkeypatch.setattr(broker_ctl._bridge, "get_instance",
                            lambda: self._Ea(healthy=True, last_seen=0))

        assert broker_ctl.ea_seconds_since_last_seen() is None

    def test_it_measures_the_gap_since_the_ea_last_spoke(self, monkeypatch):
        import time

        monkeypatch.setattr(broker_ctl._bridge, "get_instance",
                            lambda: self._Ea(healthy=True, last_seen=time.time() - 30))

        assert 29 <= broker_ctl.ea_seconds_since_last_seen() <= 31


class TestPushingToTheEa:
    """Both pushes answer False rather than raising when no EA is connected —
    "in which case nothing was sent and the saved values apply on the next
    signal instead". A page that got an exception here would show an error for
    a situation that is normal."""

    def test_a_template_push_with_no_ea_connected_reports_false(self, monkeypatch):
        scheduled = _Recorder()
        monkeypatch.setattr(broker_ctl._bridge, "get_instance", lambda: None)
        monkeypatch.setattr(broker_ctl._bridge, "schedule_push_template", scheduled)

        assert broker_ctl.push_template("Grid Runner", {"lots": 0.05}) is False
        assert scheduled.calls == [], "it sent a template to nothing"

    def test_a_template_push_reaches_the_connected_ea(self, monkeypatch):
        ea = object()
        scheduled = _Recorder()
        monkeypatch.setattr(broker_ctl._bridge, "get_instance", lambda: ea)
        monkeypatch.setattr(broker_ctl._bridge, "schedule_push_template", scheduled)

        assert broker_ctl.push_template("Grid Runner", {"lots": 0.05}) is True
        assert scheduled.calls == [((ea, "Grid Runner", {"lots": 0.05}), {})]

    def test_a_global_config_push_with_no_ea_reports_false(self, monkeypatch):
        monkeypatch.setattr(broker_ctl._bridge, "get_instance", lambda: None)

        assert _run(broker_ctl.push_global_config()) is False

    def test_a_global_config_push_reaches_the_connected_ea(self, monkeypatch):
        pushed = _AsyncRecorder()

        class _Ea:
            push_global_config = pushed

        monkeypatch.setattr(broker_ctl._bridge, "get_instance", lambda: _Ea())

        assert _run(broker_ctl.push_global_config()) is True
        assert len(pushed.calls) == 1


# ── The peer link ────────────────────────────────────────────────────────────

class _FakeClient:
    """A stand-in SyncClient. No socket, no thread."""

    def __init__(self, conn_state: str = "disconnected") -> None:
        self.conn_state = conn_state
        self.last_error = ""
        self.remote_status = {"balance": 10_250.44}
        self.remote_settings = {"bo_engine_enabled": "1"}
        self.configure = _Recorder()
        self.start = _Recorder()
        self.stop = _Recorder()
        self.get_remote_open_position = _Recorder()
        self.send_engine_control = _AsyncRecorder()
        self.send_market_order = _AsyncRecorder()
        self.request_model_snapshot = _AsyncRecorder()
        self.request_stand_down = _AsyncRecorder()
        self.request_resume = _AsyncRecorder()
        self.push_ai_config = _AsyncRecorder()


@pytest.fixture
def client(monkeypatch) -> _FakeClient:
    fake = _FakeClient()
    monkeypatch.setattr(sync_ctl._client, "get_instance", lambda: fake)
    return fake


class TestLinkState:
    def test_it_reports_the_four_fields_the_pages_render(self, client):
        client.conn_state = "connected"
        client.last_error = "none"

        assert sync_ctl.link_state() == {
            "conn_state": "connected",
            "last_error": "none",
            "remote_status": {"balance": 10_250.44},
            "remote_settings": {"bo_engine_enabled": "1"},
        }

    def test_the_dictionaries_are_copies_so_a_page_cannot_mutate_the_client(self, client):
        """Stated in the docstring and worth pinning: handing out the live
        dicts would let any page write into service state by accident."""
        state = sync_ctl.link_state()
        state["remote_status"]["balance"] = 0.0
        state["remote_settings"]["bo_engine_enabled"] = "0"

        assert client.remote_status == {"balance": 10_250.44}
        assert client.remote_settings == {"bo_engine_enabled": "1"}

    # `note_remote_setting` was tested here until 2026-09-18. It is the one
    # write that IS meant to reach the client -- without it "the next click
    # recomputes the same 'current' and the toggle sticks one-directional",
    # confirmed live with Bounce stuck OFF and Breakout stuck ON.
    #
    # It moved into services/cluster/remote_control.py, because a browser
    # cannot make that write and the only code that knows a write went to the
    # peer at all is the routing. The requirement did NOT move: it is pinned
    # harder there, by `test_the_ack_is_recorded_so_the_next_click_sees_it`
    # (two clicks, second must invert) and `test_a_refused_toggle_does_not_
    # record_anything`, which this file never covered.

    @pytest.mark.parametrize("state,expected", [
        ("connected", True),
        ("disconnected", False),
        ("connecting", False),
        ("", False),
    ])
    def test_only_connected_counts_as_connected(self, client, state, expected):
        """"The check every page front-loads before sending a command." A
        connecting link that reported True would let a command be sent into a
        socket that is not up yet."""
        client.conn_state = state

        assert sync_ctl.is_connected() is expected


class TestPeerCommands:
    """Each of these reaches a lazily-constructed singleton rather than a
    module, which is why the sweep cannot see them. They are still forwarders,
    and the arguments still have to arrive intact."""

    def test_configure_coerces_the_port_to_an_integer(self, client):
        """The settings page hands over whatever is in a text field."""
        sync_ctl.configure("10.0.0.5", "8765", "tok")

        assert client.configure.calls == [(("10.0.0.5", 8765, "tok"), {})]

    def test_start_coerces_the_port_too(self, client):
        sync_ctl.start("10.0.0.5", "8765", "tok")

        assert client.start.calls == [(("10.0.0.5", 8765, "tok"), {})]

    def test_stop_is_forwarded(self, client):
        sync_ctl.stop()

        assert client.stop.calls == [((), {})]

    def test_an_engine_control_command_is_forwarded_with_its_extras(self, client):
        _run(sync_ctl.send_engine_control("breakout", "stop", reason="operator"))

        assert client.send_engine_control.calls == [
            (("breakout", "stop"), {"reason": "operator"}),
        ]

    def test_a_market_order_keeps_its_stop_loss_as_a_keyword(self, client):
        """Money path. The stop loss must arrive as `stop_loss`, not as a
        second positional that the peer would read as something else."""
        _run(sync_ctl.send_market_order("BUY", 2421.5, lot_size=0.05))

        assert client.send_market_order.calls == [
            (("BUY",), {"stop_loss": 2421.5, "lot_size": 0.05}),
        ]

    def test_an_omitted_stop_loss_is_forwarded_as_none(self, client):
        """None means "let the engine compute an ATR stop". Dropping the
        keyword entirely would let the peer apply its own default instead."""
        _run(sync_ctl.send_market_order("BUY"))

        assert client.send_market_order.calls == [(("BUY",), {"stop_loss": None})]

    def test_stand_down_and_resume_carry_their_timeouts(self, client):
        _run(sync_ctl.request_stand_down(timeout=5.0))
        _run(sync_ctl.request_resume(timeout=7.0))

        assert client.request_stand_down.calls == [((), {"timeout": 5.0})]
        assert client.request_resume.calls == [((), {"timeout": 7.0})]

    def test_a_model_snapshot_request_carries_its_timeout(self, client):
        _run(sync_ctl.request_model_snapshot("BUY", timeout=12.0))

        assert client.request_model_snapshot.calls == [
            (("BUY",), {"timeout": 12.0}),
        ]

    def test_an_ai_config_push_forwards_the_update_it_was_given(self, client):
        updates = {"claude_model": "claude-opus-5"}

        _run(sync_ctl.push_ai_config(updates))

        assert client.push_ai_config.calls == [((updates,), {})]

    def test_the_stored_link_configuration_comes_from_the_client_class(self, monkeypatch):
        """A classmethod on SyncClient rather than the singleton — read before
        one exists, which is the point: the settings page shows the saved host
        and port on a node that has never connected."""
        monkeypatch.setattr(sync_ctl._client.SyncClient, "load_config",
                            staticmethod(lambda: ("10.0.0.5", 8765, "tok")))

        assert sync_ctl.load_config() == ("10.0.0.5", 8765, "tok")

    def test_a_remote_position_lookup_is_forwarded_and_its_answer_returned(self, client):
        answer = {"ticket": 123456}
        client.get_remote_open_position.result = answer

        assert sync_ctl.get_remote_open_position(123456) is answer
        assert client.get_remote_open_position.calls == [((123456,), {})]


class TestTheSyncServer:
    """"init() and start() are one operation from the page's point of view, and
    keeping them one here is the point: the page never holds the SyncServer, so
    it cannot start a half-built one or stash a stale handle.\""""

    def test_starting_builds_the_server_then_starts_it(self, monkeypatch):
        started = _AsyncRecorder()

        class _Srv:
            start = started

        built = _Recorder(result=_Srv())
        monkeypatch.setattr(sync_ctl._server, "init", built)

        _run(sync_ctl.server_start("0.0.0.0", "8765", "tok", main_engine="E"))

        assert built.calls == [((), {"main_engine": "E", "breakout_engine": None,
                                     "bounce_engine": None, "re_engine": None})]
        assert started.calls == [(("0.0.0.0", 8765, "tok"), {})]

    def test_stopping_with_no_server_is_a_no_op_not_a_crash(self, monkeypatch):
        """A page can toggle Local/Remote twice; the second stop has nothing to
        stop."""
        monkeypatch.setattr(sync_ctl._server, "get_instance", lambda: None)

        assert _run(sync_ctl.server_stop()) is None

    def test_stopping_a_running_server_stops_it(self, monkeypatch):
        stopped = _AsyncRecorder()

        class _Srv:
            stop = stopped

        monkeypatch.setattr(sync_ctl._server, "get_instance", lambda: _Srv())

        _run(sync_ctl.server_stop())

        assert len(stopped.calls) == 1

    def test_is_running_reflects_whether_one_was_built(self, monkeypatch):
        monkeypatch.setattr(sync_ctl._server, "get_instance", lambda: None)
        assert sync_ctl.server_is_running() is False

        monkeypatch.setattr(sync_ctl._server, "get_instance", lambda: object())
        assert sync_ctl.server_is_running() is True


# ── Small re-exports and coercions ───────────────────────────────────────────

class TestReExports:
    def test_the_version_comes_from_the_version_module(self, monkeypatch):
        monkeypatch.setattr(system_ctl._vh, "__version__", "9.9.9")

        assert system_ctl.app_version() == "9.9.9"

    def test_the_release_list_comes_from_the_version_module(self, monkeypatch):
        entries = [{"version": "9.9.9"}]
        monkeypatch.setattr(system_ctl._vh, "RELEASES", entries)

        assert system_ctl.releases() is entries

    def test_debug_mode_is_reported_as_a_bool_not_the_raw_value(self, monkeypatch):
        """The frontend shows a hint on this; a truthy string would render the
        same but compare differently everywhere else."""
        monkeypatch.setattr(auth_ctl._config, "is_debug", lambda: "yes")

        assert auth_ctl.is_debug() is True

    def test_debug_off_is_false(self, monkeypatch):
        monkeypatch.setattr(auth_ctl._config, "is_debug", lambda: None)

        assert auth_ctl.is_debug() is False


class TestBrokerTimestampToLocalDate:
    """Two arities, because the caller either has a configured broker offset or
    wants the service's own default. Passing None through would be read as "the
    offset is zero", which is a different day at the boundary."""

    def test_no_offset_given_lets_the_service_choose(self, monkeypatch):
        seen = _Recorder(result=dt.date(2026, 6, 15))
        monkeypatch.setattr(history_ctl._fmt, "broker_ts_to_local_date", seen)

        assert history_ctl.broker_ts_to_local_date(1_750_000_000.0) == dt.date(2026, 6, 15)
        assert seen.calls == [((1_750_000_000.0,), {})]

    def test_an_offset_of_zero_is_passed_through_not_treated_as_missing(self, monkeypatch):
        """The boundary that matters: 0 is a real offset (UTC) and must not be
        confused with "unset"."""
        seen = _Recorder(result=dt.date(2026, 6, 15))
        monkeypatch.setattr(history_ctl._fmt, "broker_ts_to_local_date", seen)

        history_ctl.broker_ts_to_local_date(1_750_000_000.0, 0)

        assert seen.calls == [((1_750_000_000.0, 0), {})]

    def test_a_real_offset_is_forwarded(self, monkeypatch):
        seen = _Recorder(result=dt.date(2026, 6, 15))
        monkeypatch.setattr(history_ctl._fmt, "broker_ts_to_local_date", seen)

        history_ctl.broker_ts_to_local_date(1_750_000_000.0, 180)

        assert seen.calls == [((1_750_000_000.0, 180), {})]


class TestReaderIsConfigured:
    """The owner-reported bug this function exists to fix: the view tested
    `status["connected"] or status["authenticated"]`, and the reader's status
    dict carries neither key. Both lookups returned None, so the Getting
    Started row was red on every install (2026-09-02)."""

    def test_a_connected_reader_is_configured(self):
        from backend.src.services.telegram.reader_common import AUTH_CONNECTED

        assert tg_ctl.reader_is_configured({"auth_state": AUTH_CONNECTED}) is True

    def test_a_reconnecting_reader_still_counts(self):
        """"A dropped link that is retrying is a configured install; telling
        the user to go and set it up is worse than saying nothing.\""""
        from backend.src.services.telegram.reader_common import AUTH_RECONNECTING

        assert tg_ctl.reader_is_configured({"auth_state": AUTH_RECONNECTING}) is True

    def test_a_reader_that_has_never_authenticated_is_not_configured(self):
        assert tg_ctl.reader_is_configured({"auth_state": "NEEDS_CODE"}) is False

    def test_the_keys_the_old_view_used_are_not_what_decides(self):
        """The regression test for the actual bug. A status dict carrying the
        old keys and NOT auth_state must read as unconfigured — if this ever
        returns True, somebody has reintroduced the lookup that was wrong."""
        assert tg_ctl.reader_is_configured({"connected": True, "authenticated": True}) is False

    def test_an_empty_status_is_not_configured(self):
        assert tg_ctl.reader_is_configured({}) is False


class TestTheOrbReportForTheScheduledEmail:
    """No runtime is not the same answer as a broken one.

    The controller returns None when there is no runtime yet (a headless node,
    or a button pressed before startup finishes) and lets a failure INSIDE the
    report propagate, because "the page says something different for each, and
    flattening both to None would make a broken bridge look like a quiet
    morning".
    """

    def test_no_runtime_yet_gives_none_rather_than_an_error(self, monkeypatch):
        monkeypatch.setattr(notifications_ctl, "_get_engine", lambda: None)

        assert _run(notifications_ctl.build_orb_report()) is None

    def test_the_runtime_builds_the_report(self, monkeypatch):
        built = _AsyncRecorder(result={"range_high": 2441.0})

        class _Engine:
            build_orb_report = built

        monkeypatch.setattr(notifications_ctl, "_get_engine", lambda: _Engine())

        assert _run(notifications_ctl.build_orb_report()) == {"range_high": 2441.0}

    def test_a_failure_inside_the_report_propagates(self, monkeypatch):
        """The distinction the docstring insists on: "no bridge" must not look
        like "no report today"."""
        class _Engine:
            async def build_orb_report(self):
                raise RuntimeError("no bridge")

        monkeypatch.setattr(notifications_ctl, "_get_engine", lambda: _Engine())

        with pytest.raises(RuntimeError, match="no bridge"):
            _run(notifications_ctl.build_orb_report())

    def test_the_runtime_is_fetched_through_the_deferred_helper(self, monkeypatch):
        """`_get_engine` imports the composition root inside the function.
        "Importing the composition root at module scope would have every
        importer of this controller pull the whole application graph." Asserted
        so a later tidy-up that hoists the import has to change this too."""
        import backend.src.app as app_module

        marker = object()
        monkeypatch.setattr(app_module, "get_engine", lambda: marker)

        assert notifications_ctl._get_engine() is marker


def test_restarting_the_app_goes_through_the_runtime(monkeypatch):
    """"The runtime owns the restart — it holds the bot offset that must be
    persisted first — so this forwards to it rather than duplicating the
    sequence, and stops the page reaching into `engine._cmd_restart_app`."

    Not in the forwarding sweep because it supplies an argument of its own
    (`restart_app([])`), and a sweep that asserts "what went in came out"
    cannot check a literal the controller invented.
    """
    restarted = _AsyncRecorder(result="restarting")

    class _Engine:
        restart_app = restarted

    assert _run(remote_node_ctl.restart_app(_Engine())) == "restarting"
    assert restarted.calls == [(([],), {})], (
        "the runtime must be asked to restart with no extra arguments"
    )


def test_validate_signal_forwards_to_the_parser(monkeypatch):
    """A bare-name import rather than a module alias, which is why the sweep
    cannot resolve it. Same contract: in unchanged, out unchanged."""
    from backend.src.services.signals import parser as _parser

    complaints = ["stop loss is on the wrong side of entry"]
    seen = _Recorder(result=complaints)
    monkeypatch.setattr(_parser, "validate_signal", seen)

    signal = {"direction": "BUY"}

    assert trading_ctl.validate_signal(signal) is complaints
    assert seen.calls == [((signal,), {})]
