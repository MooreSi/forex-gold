"""The VPS heartbeat and the Mac-unreachable watchdog.

Under `centralized_signal_gen_enabled` the VPS stops generating its own signals
entirely and depends on the Mac forwarding every trade. So a dropped Mac link
does not mean a missed sync tick — it means **zero new trades until it is
back**, silently. The watchdog is the only thing that says so.

It is also the loop that carries the most conditions, and every one of them
exists to stop a false alarm rather than to catch a real one:

    off unless centralized generation is on
    off unless this node is the active trader
    off until the Mac has connected at least once this run
    off below the 90s gap threshold
    once per outage (debounce)
    at most once per 600s regardless of how many outages (the flapping floor)

That last one has a date on it: 2026-07-13, six-plus "Mac unreachable" emails
in twenty minutes, because each brief reconnect cleared the debounce before the
next drop.

This module was also where 11 of bugs/018's 15 undefined names lived, so these
tests double as proof the loops actually run now — a name resolving is not the
same as a function working.

No sockets, no database, no email. The loops are driven one iteration at a
time by a sleep that stops them.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.sync import _telemetry as tel

pytestmark = pytest.mark.asyncio


class _Stop(Exception):
    """Raised by the patched sleep to end a `while True` loop after N passes."""


def _stop_after_sleeps(monkeypatch, n: int):
    """Raise out of the loop on the nth `asyncio.sleep`.

    How many times the body runs depends on the loop's shape, so the call sites
    say which they are rather than this helper guessing:

      * `_heartbeat_loop` is body-then-sleep -> n sleeps means n bodies
      * `_liveness_watchdog_loop` is sleep-then-body -> n sleeps means n-1
    """
    state = {"n": 0}

    async def _sleep(_secs):
        state["n"] += 1
        if state["n"] >= n:
            raise _Stop
    monkeypatch.setattr(tel.asyncio, "sleep", _sleep)
    return state


class _Node(tel.TelemetryMixin):
    """A SyncServer stripped to what the telemetry loops touch."""

    def __init__(self):
        self._main_engine = None
        self._last_seen_ts = 0.0
        self._liveness_alerted = False
        self._last_liveness_alert_sent_ts = 0.0
        self.broadcast: list = []

    async def _broadcast(self, msg):
        self.broadcast.append(msg)

    def _sub_engines(self):
        return {}


@pytest.fixture
def node():
    return _Node()


@pytest.fixture
def alerts(monkeypatch):
    """Capture what the watchdog would have sent."""
    sent = {"telegram": [], "email": []}

    from backend.src.services.telegram import alerts as tg
    from backend.src.services.notifications import email_service

    async def _tg(msg, *a, **kw):
        sent["telegram"].append(msg)

    async def _email(subject, body, cfg):
        sent["email"].append(subject)

    monkeypatch.setattr(tg, "send_message", _tg)
    monkeypatch.setattr(email_service, "send_email", _email)
    return sent


@pytest.fixture
def db(monkeypatch):
    """Drive the two database reads the watchdog makes."""
    box = {"settings": {"centralized_signal_gen_enabled": 1},
           "active_trader": tel.TRADER_REMOTE_VPS}

    async def _to_db_thread(fn, *a, **kw):
        return fn(*a, **kw)

    monkeypatch.setattr(tel.db_module, "to_db_thread", _to_db_thread)
    monkeypatch.setattr(tel.db_module, "get_risk_settings", lambda: box["settings"])
    monkeypatch.setattr(tel.db_module, "get_active_trader", lambda: box["active_trader"])
    monkeypatch.setattr(tel.db_module, "get_email_config", lambda: {})
    return box


async def _run(loop_coro):
    try:
        await loop_coro
    except _Stop:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Heartbeat
# ─────────────────────────────────────────────────────────────────────────────

class TestTheHeartbeat:

    async def test_it_broadcasts_a_status_heartbeat(self, node, monkeypatch, db):
        _stop_after_sleeps(monkeypatch, 1)      # one beat

        await _run(node._heartbeat_loop())

        assert len(node.broadcast) == 1
        assert node.broadcast[0]["type"] == tel.MSG_STATUS_HEARTBEAT

    async def test_it_keeps_going_when_a_beat_fails(self, node, monkeypatch, db):
        """A loop that dies on one bad payload takes the Mac's whole view of
        this node with it, and nothing says so."""
        calls = {"n": 0}

        async def _payload(_self=None):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("engine not ready yet")
            return {"ts": 1.0}
        monkeypatch.setattr(type(node), "_status_payload", _payload)
        _stop_after_sleeps(monkeypatch, 2)      # two beats

        await _run(node._heartbeat_loop())

        assert calls["n"] == 2
        assert len(node.broadcast) == 1, "the beat after the failure never went out"

    async def test_the_payload_carries_what_the_mac_renders(self, node, db):
        payload = await node._status_payload()

        assert "ts" in payload

    async def test_a_full_payload_names_the_active_trader_and_engines(
            self, node, db, monkeypatch):
        class _Engine:
            def get_open_trades(self):
                return []

            async def get_mt5_account(self):
                return {"balance": 1234.5, "equity": 1200.0}
        node._main_engine = _Engine()

        payload = await node._status_payload()

        assert payload["balance"] == 1234.5
        assert payload["equity"] == 1200.0
        assert payload["open_positions"] == []
        assert payload["active_trader"] == tel.TRADER_REMOTE_VPS


# ─────────────────────────────────────────────────────────────────────────────
# Liveness watchdog
# ─────────────────────────────────────────────────────────────────────────────

class TestTheWatchdogAlertsWhenItShould:

    async def test_a_long_silence_alerts_on_both_channels(self, node, monkeypatch,
                                                          db, alerts):
        """The positive control everything below is measured against."""
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)      # one watchdog pass

        await _run(node._liveness_watchdog_loop())

        assert len(alerts["telegram"]) == 1
        assert "Mac unreachable" in alerts["telegram"][0]
        assert len(alerts["email"]) == 1

    async def test_the_alert_says_how_long_it_has_been(self, node, monkeypatch,
                                                       db, alerts):
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)      # one watchdog pass

        await _run(node._liveness_watchdog_loop())

        assert "500s" in alerts["telegram"][0] or "499s" in alerts["telegram"][0]

    async def test_a_failed_email_does_not_stop_the_loop_or_lose_the_telegram(
            self, node, monkeypatch, db, alerts):
        from backend.src.services.notifications import email_service

        async def _boom(*_a, **_kw):
            raise OSError("smtp is down")
        monkeypatch.setattr(email_service, "send_email", _boom)
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)      # one watchdog pass

        await _run(node._liveness_watchdog_loop())

        assert len(alerts["telegram"]) == 1


class TestTheWatchdogStaysQuietWhenItShould:
    """Each of these is a false alarm that would have been sent."""

    async def test_not_when_centralized_generation_is_off(self, node, monkeypatch,
                                                          db, alerts):
        db["settings"] = {"centralized_signal_gen_enabled": 0}
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)      # one watchdog pass

        await _run(node._liveness_watchdog_loop())

        assert alerts["telegram"] == []

    async def test_not_when_this_node_is_not_the_active_trader(self, node,
                                                              monkeypatch, db,
                                                              alerts):
        db["active_trader"] = "mac"
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)      # one watchdog pass

        await _run(node._liveness_watchdog_loop())

        assert alerts["telegram"] == []

    async def test_not_before_the_mac_has_EVER_connected_this_run(
            self, node, monkeypatch, db, alerts):
        """A fresh boot has a zero last-seen. Alerting on it would fire on
        every restart."""
        node._last_seen_ts = 0.0
        _stop_after_sleeps(monkeypatch, 2)      # one watchdog pass

        await _run(node._liveness_watchdog_loop())

        assert alerts["telegram"] == []

    async def test_not_for_a_gap_under_the_threshold(self, node, monkeypatch,
                                                     db, alerts):
        node._last_seen_ts = tel.time.time() - (tel._LIVENESS_ALERT_THRESHOLD_S - 5)
        _stop_after_sleeps(monkeypatch, 2)      # one watchdog pass

        await _run(node._liveness_watchdog_loop())

        assert alerts["telegram"] == []

    async def test_not_twice_for_the_same_outage(self, node, monkeypatch, db,
                                                 alerts):
        """`_liveness_alerted` is the per-outage debounce, and it is a SEPARATE
        guard from the re-alert floor below.

        Proving that needs a case where the floor cannot be doing the work:
        each watchdog pass here is 700s after the last, so the floor (600s)
        allows a second alert and only the debounce stops it. Without the time
        advance, removing `or self._liveness_alerted` leaves every test green
        -- confirmed by mutation, which is why this test is written this way.
        """
        clock = {"t": 1_000_000.0}
        monkeypatch.setattr(tel.time, "time", lambda: clock["t"])
        node._last_seen_ts = clock["t"] - 500

        async def _sleep(_secs):
            clock["t"] += 700.0          # past the 600s re-alert floor
            if clock["t"] > 1_000_000.0 + 3000:
                raise _Stop
        monkeypatch.setattr(tel.asyncio, "sleep", _sleep)

        await _run(node._liveness_watchdog_loop())

        assert len(alerts["telegram"]) == 1, (
            "the outage was alerted more than once. The re-alert floor cannot "
            "be what stops it here -- each pass is 700s apart -- so this is "
            "the _liveness_alerted debounce."
        )

    async def test_not_more_than_once_per_realert_window_across_outages(
            self, node, monkeypatch, db, alerts):
        """2026-07-13: six-plus emails in twenty minutes. A flapping link
        reconnects, which clears the debounce, and the next drop alerts again.
        This floor is what caps it regardless of how many separate outages
        occur."""
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)
        await _run(node._liveness_watchdog_loop())
        assert len(alerts["telegram"]) == 1

        # The link flaps: a reconnect clears the debounce, then it drops again.
        node._liveness_alerted = False
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)
        await _run(node._liveness_watchdog_loop())

        assert len(alerts["telegram"]) == 1, (
            "a second alert went out inside the re-alert floor -- this is the "
            "2026-07-13 email storm"
        )

    async def test_it_DOES_alert_again_once_the_window_has_passed(
            self, node, monkeypatch, db, alerts):
        """Control for the floor: an outage a long time later is real news."""
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)
        await _run(node._liveness_watchdog_loop())

        node._liveness_alerted = False
        node._last_liveness_alert_sent_ts = (
            tel.time.time() - tel._LIVENESS_MIN_REALERT_INTERVAL_S - 1
        )
        node._last_seen_ts = tel.time.time() - 500
        _stop_after_sleeps(monkeypatch, 2)
        await _run(node._liveness_watchdog_loop())

        assert len(alerts["telegram"]) == 2

    async def test_a_database_error_does_not_kill_the_watchdog(
            self, node, monkeypatch, db, alerts):
        calls = {"n": 0}

        def _settings():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("database is locked")
            return {"centralized_signal_gen_enabled": 0}
        monkeypatch.setattr(tel.db_module, "get_risk_settings", _settings)
        _stop_after_sleeps(monkeypatch, 3)      # two watchdog passes

        await _run(node._liveness_watchdog_loop())

        assert calls["n"] == 2, "the watchdog died on one bad read"


class TestResourceUsage:
    async def test_it_returns_a_dict_even_without_psutil(self, monkeypatch):
        """Sent with every 3s heartbeat. If it raises, the beat is lost."""
        import builtins
        real_import = builtins.__import__

        def _no_psutil(name, *a, **kw):
            if name == "psutil":
                raise ImportError("not installed")
            return real_import(name, *a, **kw)
        monkeypatch.setattr(builtins, "__import__", _no_psutil)

        assert isinstance(tel._get_resource_usage(), dict)
