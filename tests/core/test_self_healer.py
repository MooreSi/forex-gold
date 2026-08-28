"""The self-healer: the only thing in this app that restarts a process on its
own initiative, and it had no tests.

Every 90 seconds it scans recent log lines for known failure shapes and, past a
threshold, acts -- restarting the bridge process, forcing a Telegram
reconnect, or alerting. Three guards stand between "an error appeared in the
log" and "something got restarted", and each exists because of a specific way
this goes wrong:

  THRESHOLD  three occurrences in a five-minute window. One transient error is
             already handled by the ordinary watchdogs; reacting to it means
             restarting the bridge over a single refused connection.
  GRACE      bridge_offline is ignored for the first ~5.5 minutes after start.
             A full VPS reboot needs MT5 to cold-start and log in before the
             bridge answers -- observed taking up to 150s. Without this, every
             reboot's normal warmup reads as an outage and triggers a redundant
             restart plus an alert email on top of the watchdog's own.
  COOLDOWN   ten minutes between heals for the same condition, so a condition
             that persists is not restarted every 90 seconds forever.

NOTHING IS SENT AND NOTHING IS RESTARTED by these tests. The notification
function is replaced, and the engine is a fake that records what it was asked
to do. A test here that reached the real _send_heal_notification would send the
owner a Telegram message and an email.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.health import self_healer as sh




class _Engine:
    def __init__(self, started_at=None, launch=True, reader=None):
        self._started_at = started_at
        self._launch = launch
        self._tg_reader = reader
        self.bridge_starts = 0

    async def start_bridge_process(self):
        self.bridge_starts += 1
        if isinstance(self._launch, Exception):
            raise self._launch
        return self._launch


class _Reader:
    def __init__(self, result=None):
        self.result = result if result is not None else {"ok": True}
        self.reconnects = 0

    async def reconnect(self):
        self.reconnects += 1
        return self.result


@pytest.fixture(autouse=True)
def no_notifications(monkeypatch):
    """Nothing leaves the machine. Records what WOULD have been sent."""
    sent: list[tuple[str, str]] = []

    async def _fake(condition, action):
        sent.append((condition, action))

    monkeypatch.setattr(sh, "_send_heal_notification", _fake)
    return sent


@pytest.fixture
def log_lines(monkeypatch):
    """Feeds the scanner a fixed set of log lines instead of reading a file."""
    box = {"lines": []}
    monkeypatch.setattr(sh, "_read_recent_log_lines",
                        lambda window: list(box["lines"]))
    return box


def _lines(text: str, n: int):
    now = time.time()
    return [(now, f"2026-08-28 19:00:00 ERROR mod — {text}") for _ in range(n)]


class TestThePatternsMatchRealLogLines:
    """Written from lines this app actually emits. A pattern that matches
    nothing makes the whole monitor a no-op that still logs 'Started'."""

    @pytest.mark.parametrize("line", [
        "HTTPConnectionPool: Connection refused localhost:9000",
        "EA bridge offline — no heartbeat",
        "bridge did not respond within 10s",
    ])
    def test_bridge_offline(self, line):
        pat = dict(sh._PATTERNS)["bridge_offline"]
        assert pat.search(line), line

    @pytest.mark.parametrize("line", [
        "[Watchdog] Telethon client dropped — reconnecting",
        "telethon disconnected unexpectedly",
        "ConnectionResetError: [Errno 54] Connection reset by peer",
    ])
    def test_telegram_dropped(self, line):
        pat = dict(sh._PATTERNS)["telegram_dropped"]
        assert pat.search(line), line

    @pytest.mark.parametrize("line", [
        "Exception in monitor loop: KeyError",
        "Error in position monitor — aborting",
    ])
    def test_monitor_crash(self, line):
        pat = dict(sh._PATTERNS)["monitor_crash"]
        assert pat.search(line), line

    def test_an_ordinary_line_matches_nothing(self):
        """A pattern loose enough to match normal traffic would restart the
        bridge on a healthy system."""
        line = "2026-08-28 19:00:00 INFO httpx — GET /tick/XAUUSD 200 OK"
        for _key, pat in sh._PATTERNS:
            assert not pat.search(line), f"{_key} matched an ordinary line"


@pytest.mark.asyncio
class TestTheThreshold:
    async def test_below_the_threshold_nothing_happens(self, log_lines, no_notifications):
        """Two refused connections is the watchdog's business, not a
        restart."""
        log_lines["lines"] = _lines("Connection refused localhost:9000",
                                    sh._THRESHOLD - 1)
        engine = _Engine(started_at=None)

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 0
        assert no_notifications == []

    async def test_at_the_threshold_it_acts(self, log_lines, no_notifications):
        log_lines["lines"] = _lines("Connection refused localhost:9000",
                                    sh._THRESHOLD)
        engine = _Engine(started_at=None)

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 1
        assert no_notifications[0][0] == "bridge_offline"

    async def test_an_empty_log_does_nothing(self, log_lines, no_notifications):
        log_lines["lines"] = []
        engine = _Engine()

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 0


@pytest.mark.asyncio
class TestTheStartupGrace:
    async def test_bridge_offline_is_IGNORED_during_warmup(self, log_lines, no_notifications):
        """A full reboot needs MT5 to cold-start and log in -- up to 150s
        observed. Without this, every reboot triggers a redundant restart and
        an alert email on top of the watchdog's own."""
        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        engine = _Engine(started_at=time.monotonic())

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 0
        assert no_notifications == []

    async def test_it_acts_once_the_grace_has_passed(self, log_lines, no_notifications):
        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        engine = _Engine(started_at=time.monotonic() - sh._BRIDGE_STARTUP_GRACE - 1)

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 1

    async def test_the_grace_does_NOT_suppress_other_conditions(self, log_lines,
                                                                no_notifications):
        """It is specific to bridge_offline. A Telegram drop during startup is
        still a Telegram drop."""
        log_lines["lines"] = _lines("telethon disconnected", 10)
        reader = _Reader()
        engine = _Engine(started_at=time.monotonic(), reader=reader)

        await sh.SelfHealer(engine)._scan()

        assert reader.reconnects == 1

    async def test_an_engine_with_no_start_time_is_not_in_grace(self, log_lines,
                                                                no_notifications):
        """getattr default is None, and None must not read as "just started"
        and suppress healing forever."""
        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        engine = _Engine(started_at=None)

        await sh.SelfHealer(engine)._scan()

        assert engine.bridge_starts == 1


class TestTheTunablesAgreeWithEachOther:
    def test_the_grace_OUTLASTS_the_lookback_window(self):
        """Not arbitrary. If the grace were shorter than the window, the first
        scan after it lifted would still see the tail of the startup burst and
        heal on it -- the comment in the source says exactly this."""
        assert sh._BRIDGE_STARTUP_GRACE > sh._WINDOW_SECONDS

    def test_the_cooldown_outlasts_the_poll_interval(self):
        """Otherwise the cooldown never bites and a persistent condition is
        healed on every scan."""
        assert sh._COOLDOWN > sh._POLL_INTERVAL


@pytest.mark.asyncio
class TestTheCooldown:
    async def test_it_does_not_heal_the_same_condition_twice_in_a_row(
            self, log_lines, no_notifications):
        """The condition persists between scans by nature -- the log lines
        that triggered it are still in the window. Without the cooldown the
        bridge would be restarted every 90 seconds."""
        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        engine = _Engine(started_at=None)
        healer = sh.SelfHealer(engine)

        await healer._scan()
        await healer._scan()

        assert engine.bridge_starts == 1

    async def test_it_heals_again_once_the_cooldown_expires(self, log_lines,
                                                            no_notifications):
        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        engine = _Engine(started_at=None)
        healer = sh.SelfHealer(engine)

        await healer._scan()
        healer._last_heal["bridge_offline"] = time.time() - sh._COOLDOWN - 1
        await healer._scan()

        assert engine.bridge_starts == 2

    async def test_the_cooldown_is_PER_CONDITION(self, log_lines, no_notifications):
        """A bridge restart must not silence a Telegram drop that happens in
        the same window."""
        engine = _Engine(started_at=None, reader=_Reader())
        healer = sh.SelfHealer(engine)

        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        await healer._scan()

        log_lines["lines"] = _lines("telethon disconnected", 10)
        await healer._scan()

        assert engine.bridge_starts == 1
        assert engine._tg_reader.reconnects == 1

    async def test_the_cooldown_HOLDS_EVEN_IF_NOTIFYING_RAISES(self, log_lines,
                                                               monkeypatch):
        """The cooldown is recorded BEFORE the heal runs, and that ordering is
        what this pins. _heal swallows its own errors, so nothing else can
        tell the two orderings apart -- found by mutation, which survived
        moving the assignment after the await.

        It matters because notifying is the one step in _heal not wrapped in
        its own try. A Telegram outage that made it raise would, with the
        assignment after the await, leave the cooldown unset and restart the
        bridge on every 90-second scan for as long as the outage lasted."""
        async def _raises(condition, action):
            raise RuntimeError("telegram is down")

        monkeypatch.setattr(sh, "_send_heal_notification", _raises)
        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        engine = _Engine(started_at=None)
        healer = sh.SelfHealer(engine)

        with pytest.raises(RuntimeError):
            await healer._scan()
        assert healer._last_heal.get("bridge_offline"), "the cooldown was not recorded"

        # Second scan: the cooldown must stop it acting again.
        await healer._scan()
        assert engine.bridge_starts == 1

    async def test_the_cooldown_starts_even_if_the_heal_fails(self, log_lines,
                                                              no_notifications):
        """Otherwise a heal that raises every time retries on every scan."""
        log_lines["lines"] = _lines("Connection refused localhost:9000", 10)
        engine = _Engine(started_at=None, launch=RuntimeError("wine is not installed"))
        healer = sh.SelfHealer(engine)

        await healer._scan()
        await healer._scan()

        assert engine.bridge_starts == 1


@pytest.mark.asyncio
class TestTheHealActions:
    async def test_a_successful_bridge_restart_is_reported_as_such(self, no_notifications):
        engine = _Engine(launch=True)

        await sh.SelfHealer(engine)._heal("bridge_offline")

        assert "restarted" in no_notifications[0][1].lower()

    async def test_a_FAILED_LAUNCH_is_reported_as_a_failure(self, no_notifications):
        """start_bridge_process returning False is not an exception, and
        reporting it as success would tell the user the bridge is back when
        it is not."""
        engine = _Engine(launch=False)

        await sh.SelfHealer(engine)._heal("bridge_offline")

        action = no_notifications[0][1]
        assert "failed" in action.lower()

    async def test_a_raising_restart_reports_the_reason(self, no_notifications):
        engine = _Engine(launch=RuntimeError("wine is not installed"))

        await sh.SelfHealer(engine)._heal("bridge_offline")

        assert "wine is not installed" in no_notifications[0][1]

    async def test_a_telegram_reconnect_is_attempted(self, no_notifications):
        reader = _Reader({"ok": True})
        engine = _Engine(reader=reader)

        await sh.SelfHealer(engine)._heal("telegram_dropped")

        assert reader.reconnects == 1
        assert "reconnected" in no_notifications[0][1].lower()

    async def test_a_failed_reconnect_reports_the_error(self, no_notifications):
        engine = _Engine(reader=_Reader({"ok": False, "error": "auth expired"}))

        await sh.SelfHealer(engine)._heal("telegram_dropped")

        assert "auth expired" in no_notifications[0][1]

    async def test_no_reader_is_reported_not_crashed(self, no_notifications):
        engine = _Engine(reader=None)

        await sh.SelfHealer(engine)._heal("telegram_dropped")

        assert "not accessible" in no_notifications[0][1].lower()

    async def test_a_monitor_crash_only_RECOMMENDS_a_restart(self, no_notifications):
        """It does not restart the app itself. That is deliberate -- a
        position monitor crash with open trades wants a human."""
        engine = _Engine()

        await sh.SelfHealer(engine)._heal("monitor_crash")

        action = no_notifications[0][1]
        assert "recommend" in action.lower()
        assert engine.bridge_starts == 0

    async def test_an_unknown_condition_takes_NO_action(self, no_notifications):
        engine = _Engine()

        await sh.SelfHealer(engine)._heal("something_new")

        assert engine.bridge_starts == 0
        assert "no action taken" in no_notifications[0][1].lower()

    async def test_every_heal_notifies(self, no_notifications):
        """A silent restart is worse than none -- the user finds a bridge
        that reconnected for no reason they can see."""
        for condition in ("bridge_offline", "telegram_dropped",
                          "monitor_crash", "unknown_thing"):
            no_notifications.clear()
            await sh.SelfHealer(_Engine())._heal(condition)
            assert len(no_notifications) == 1, f"{condition} did not notify"
