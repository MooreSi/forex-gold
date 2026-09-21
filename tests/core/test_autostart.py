"""Covers the auto-restart watchdog: forex_trader/core/core_autostart.py and
the tools/watchdog.py tick that the OS scheduler runs.

No test registers a real LaunchAgent or Scheduled Task, spawns a real process,
or writes outside tmp_path -- launchctl/schtasks and Popen are mocked, and the
flag-file paths are redirected per test. Running the suite must never change
whether the developer's own app is supervised.

The behaviours worth protecting here are the ones that make Stop mean stop and
keep a second instance from ever being launched on top of a running app; see
the module docstring in core_autostart.py for why supervision is a poller and
not launchd's KeepAlive.
"""
import importlib.util
import sys
import time
from pathlib import Path
from unittest import mock

import pytest

from backend.src.services.positions import core_autostart as autostart

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent


def _load_watchdog():
    """Load tools/watchdog.py as a module (it is a script, not a package)."""
    spec = importlib.util.spec_from_file_location(
        "_watchdog_under_test", _REPO_ROOT / "tools" / "watchdog.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def flags(tmp_path, monkeypatch):
    """Redirect both flag files into tmp_path for the duration of a test."""
    armed = tmp_path / "watchdog.armed"
    last = tmp_path / "watchdog.last_launch"
    # The plist too: reading the developer's real ~/Library/LaunchAgents entry
    # would make points_at_this_checkout() answer about THEIR machine, so a
    # developer who has auto-restart enabled from another checkout would see
    # these tests fail for a reason that has nothing to do with the code.
    plist = tmp_path / "LaunchAgents" / f"{autostart.LAUNCHD_LABEL}.plist"
    monkeypatch.setattr(autostart, "ARMED_FLAG", armed)
    monkeypatch.setattr(autostart, "LAST_LAUNCH_FILE", last)
    monkeypatch.setattr(autostart, "_PLIST_PATH", plist)
    return {"armed": armed, "last": last, "plist": plist}


# ── Armed flag ────────────────────────────────────────────────────────────────

def test_arm_creates_flag_and_is_armed_reports_it(flags):
    assert autostart.is_armed() is False
    autostart.arm()
    assert flags["armed"].exists()
    assert autostart.is_armed() is True


def test_arm_is_idempotent(flags):
    autostart.arm()
    autostart.arm()
    assert autostart.is_armed() is True


def test_disarm_removes_flag(flags):
    autostart.arm()
    autostart.disarm()
    assert not flags["armed"].exists()
    assert autostart.is_armed() is False


def test_disarm_when_never_armed_does_not_raise(flags):
    autostart.disarm()  # must not raise FileNotFoundError
    assert autostart.is_armed() is False


def test_disarm_clears_the_launch_timestamp(flags):
    """A stale timestamp would make the next arm() sit out its first tick."""
    autostart.arm()
    flags["last"].write_text(str(time.time()), encoding="utf-8")
    autostart.disarm()
    assert not flags["last"].exists()


# ── enable() guards ───────────────────────────────────────────────────────────

def test_enable_refuses_on_unsupported_platform(flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    with pytest.raises(RuntimeError, match="not supported"):
        autostart.enable()
    assert autostart.is_armed() is False, "must not arm when it cannot supervise"


def test_enable_refuses_when_watchdog_script_is_missing(flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        autostart, "watchdog_script", lambda: Path("/nonexistent/watchdog.py")
    )
    with pytest.raises(RuntimeError, match="Watchdog script missing"):
        autostart.enable()
    assert autostart.is_armed() is False


def test_enable_arms_only_after_the_scheduler_entry_succeeds(flags, monkeypatch):
    """A toggle showing ON with nothing behind it is the false sense of safety
    this feature exists to remove."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        autostart, "_mac_install", mock.Mock(side_effect=RuntimeError("launchctl boom"))
    )
    with pytest.raises(RuntimeError, match="launchctl boom"):
        autostart.enable()
    assert autostart.is_armed() is False


def test_enable_installs_then_arms(flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    install = mock.Mock()
    monkeypatch.setattr(autostart, "_mac_install", install)
    autostart.enable()
    assert install.called
    assert autostart.is_armed() is True


# ── disable() ─────────────────────────────────────────────────────────────────

def test_disable_disarms_even_when_uninstall_fails(flags, monkeypatch):
    """Losing the scheduler entry is recoverable; a still-armed watchdog after
    the user turned it off is not."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "_mac_install", mock.Mock())
    autostart.enable()
    monkeypatch.setattr(
        autostart, "_mac_uninstall", mock.Mock(side_effect=RuntimeError("nope"))
    )
    autostart.disable()  # must not raise
    assert autostart.is_armed() is False


# ── sync_from_setting() — the startup re-arm ──────────────────────────────────

def test_sync_enabled_reinstalls_when_the_entry_is_gone(flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "is_installed", lambda: False)
    install = mock.Mock()
    monkeypatch.setattr(autostart, "_mac_install", install)
    autostart.sync_from_setting(True)
    assert install.called, "a lost LaunchAgent should be repaired on startup"
    assert autostart.is_armed() is True


def test_sync_enabled_rearms_without_reinstalling(flags, monkeypatch):
    """The normal path after a stop script disarmed us."""
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(autostart, "is_installed", lambda: True)
    install = mock.Mock()
    monkeypatch.setattr(autostart, "_mac_install", install)
    autostart.sync_from_setting(True)
    assert not install.called
    assert autostart.is_armed() is True


def test_sync_disabled_disarms(flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    autostart.arm()
    autostart.sync_from_setting(False)
    assert autostart.is_armed() is False


def test_sync_never_raises_so_startup_cannot_be_blocked(flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(
        autostart, "is_installed", mock.Mock(side_effect=RuntimeError("launchctl gone"))
    )
    autostart.sync_from_setting(True)  # must swallow


def test_sync_is_a_noop_on_unsupported_platforms(flags, monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    autostart.sync_from_setting(True)
    assert autostart.is_armed() is False


# ── Windows scheduler entry ───────────────────────────────────────────────────

def test_win_install_quotes_interpreter_and_script_separately(monkeypatch):
    """Install paths contain spaces ("FOREX Trader"); an unquoted /tr command
    would be parsed by Task Scheduler as separate arguments."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_win_python", lambda: r"C:\FOREX Trader\.venv\pythonw.exe")
    monkeypatch.setattr(
        autostart, "watchdog_script", lambda: Path(r"C:\FOREX Trader\tools\watchdog.py")
    )
    calls = []

    def fake(*args):
        calls.append(args)
        return mock.Mock(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(autostart, "_schtasks", fake)
    autostart._win_install()

    args = calls[0]
    tr_value = args[args.index("/tr") + 1]
    assert tr_value == r'"C:\FOREX Trader\.venv\pythonw.exe" "C:\FOREX Trader\tools\watchdog.py"'
    assert "/f" in args, "re-enabling must repair an existing task, not error"


def test_win_install_raises_on_schtasks_failure(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(autostart, "_win_python", lambda: "py.exe")
    monkeypatch.setattr(
        autostart,
        "_schtasks",
        lambda *a: mock.Mock(returncode=1, stdout="", stderr="Access is denied."),
    )
    with pytest.raises(RuntimeError, match="Access is denied"):
        autostart._win_install()


# ── The watchdog tick ─────────────────────────────────────────────────────────

@pytest.fixture
def wd(flags, monkeypatch):
    """watchdog module with a stubbed spawn, so no test starts a real app."""
    module = _load_watchdog()
    monkeypatch.setattr(module, "autostart", autostart)
    module.launches = []
    monkeypatch.setattr(
        module, "_launch", lambda: (module.launches.append(1), module._mark_launched())
    )
    return module


def test_tick_does_nothing_when_disarmed_even_if_app_is_down(wd, monkeypatch):
    """This is what makes FOREX Stop.command actually stop the app."""
    monkeypatch.setattr(wd, "_is_serving", lambda port: False)
    assert wd.main() == 0
    assert wd.launches == []


def test_tick_does_nothing_when_the_app_is_serving(wd, monkeypatch):
    autostart.arm()
    monkeypatch.setattr(wd, "_is_serving", lambda port: True)
    assert wd.main() == 0
    assert wd.launches == []


def test_tick_launches_when_armed_and_app_is_down(wd, monkeypatch):
    autostart.arm()
    monkeypatch.setattr(wd, "_is_serving", lambda port: False)
    assert wd.main() == 0
    assert len(wd.launches) == 1


def test_second_tick_inside_grace_window_does_not_double_launch(wd, monkeypatch):
    """A cold start binds its port last; without the grace window the next tick
    would stack a second instance on a still-booting app."""
    autostart.arm()
    monkeypatch.setattr(wd, "_is_serving", lambda port: False)
    wd.main()
    wd.main()
    wd.main()
    assert len(wd.launches) == 1


def test_tick_retries_once_the_grace_window_expires(wd, monkeypatch, flags):
    """A launch that failed to come up must not disable supervision forever."""
    autostart.arm()
    monkeypatch.setattr(wd, "_is_serving", lambda port: False)
    wd.main()
    stale = time.time() - (wd.LAUNCH_GRACE_SECS + 10)
    import os
    os.utime(flags["last"], (stale, stale))
    wd.main()
    assert len(wd.launches) == 2


def test_probe_reports_false_for_a_port_nothing_listens_on(wd):
    # Port 0 is never a listening service; a refused connect must read as "down".
    assert wd._is_serving(1) is False


def test_app_port_falls_back_when_config_is_unreadable(wd, monkeypatch):
    monkeypatch.setattr(
        "backend.src.config.load", mock.Mock(side_effect=RuntimeError("bad yaml"))
    )
    assert wd._app_port() == 8888


# ── The stored setting, which startup reconciles against ─────────────────────

class TestTheSettingSurvivesARestart:
    """`app.py` calls `sync_from_setting(get_app_config("auto_restart_enabled")
    == "1")` on every boot, and reconciles the OS to whatever that says.

    So installing the scheduler entry without writing the setting is worse than
    doing nothing: the watchdog works until the next restart and then **removes
    itself**, leaving a toggle that reads ON with no supervision behind it —
    the exact false sense of safety this feature exists to remove.

    The NiceGUI switch wrote the key by hand after the OS call succeeded. The
    React port calls `autostart_enable()` through a router and cannot, so the
    write belongs in the service where every caller gets it.
    """

    def test_enabling_records_it(self, monkeypatch, tmp_path):
        written = {}
        monkeypatch.setattr(autostart, "is_supported", lambda: True)
        monkeypatch.setattr(autostart, "watchdog_script", lambda: tmp_path / "w.sh")
        (tmp_path / "w.sh").write_text("#!/bin/sh\n")
        monkeypatch.setattr(autostart, "_mac_install", lambda: None)
        monkeypatch.setattr(autostart, "_win_install", lambda: None)
        monkeypatch.setattr(autostart, "arm", lambda: None)
        monkeypatch.setattr(autostart.db_module, "set_app_config",
                            lambda k, v: written.__setitem__(k, v))

        autostart.enable()

        assert written == {"auto_restart_enabled": "1"}

    def test_disabling_records_it_too(self, monkeypatch):
        written = {}
        monkeypatch.setattr(autostart, "disarm", lambda: None)
        monkeypatch.setattr(autostart, "_mac_uninstall", lambda: None)
        monkeypatch.setattr(autostart, "_win_uninstall", lambda: None)
        monkeypatch.setattr(autostart.db_module, "set_app_config",
                            lambda k, v: written.__setitem__(k, v))

        autostart.disable()

        assert written == {"auto_restart_enabled": "0"}

    def test_an_os_refusal_does_not_record_it_as_on(self, monkeypatch, tmp_path):
        """A toggle showing ON with no scheduler entry behind it is the thing
        this guards. `enable()` raises, so the setting must stay where it was."""
        written = {}
        monkeypatch.setattr(autostart, "is_supported", lambda: True)
        monkeypatch.setattr(autostart, "watchdog_script", lambda: tmp_path / "missing.sh")
        monkeypatch.setattr(autostart.db_module, "set_app_config",
                            lambda k, v: written.__setitem__(k, v))

        with pytest.raises(RuntimeError):
            autostart.enable()

        assert written == {}

    def test_a_failed_write_does_not_undo_the_install(self, monkeypatch, tmp_path):
        """The OS entry is in place; the app is supervised. Losing the record
        means it is undone at the next restart, which is bad — but raising here
        would report a failure for something that DID happen."""
        monkeypatch.setattr(autostart, "is_supported", lambda: True)
        monkeypatch.setattr(autostart, "watchdog_script", lambda: tmp_path / "w.sh")
        (tmp_path / "w.sh").write_text("#!/bin/sh\n")
        monkeypatch.setattr(autostart, "_mac_install", lambda: None)
        monkeypatch.setattr(autostart, "_win_install", lambda: None)
        monkeypatch.setattr(autostart, "arm", lambda: None)

        def _boom(k, v):
            raise RuntimeError("database locked")

        monkeypatch.setattr(autostart.db_module, "set_app_config", _boom)

        autostart.enable()   # must not raise


# ── A scheduler entry left behind by a DIFFERENT checkout ────────────────────

class TestTheEntryPointsAtThisCheckout:
    """Two checkouts of this app share one USER_DATA_DIR, and the LaunchAgent
    lives in the home directory with a fixed label — so there is exactly one
    entry for both of them, and it runs whichever checkout was enabled last.

    `_launchd_plist()` bakes that path in at enable() time and `watchdog.py`
    resolves its own ROOT from its own file, so a stale entry launches the
    OTHER app, every 120s and at login, with `--no-browser` so nothing visible
    happens. Observed 2026-09-21 on a remote Mac: the owner started the React
    checkout, the agent relaunched the NiceGUI one into the port during the
    first-run venv build, and `run.py` then refused to start against the
    single-instance lock. The dashboard that came up was the old app, from a
    folder the owner had not launched.

    `sync_from_setting()` only asked whether an entry EXISTS, so starting the
    right app could never repair it — it re-armed the wrong one instead.
    """

    @pytest.fixture
    def here(self, tmp_path, monkeypatch):
        """This checkout's watchdog script, as an existing file."""
        script = tmp_path / "here" / "tools" / "watchdog.py"
        script.parent.mkdir(parents=True)
        script.write_text("# watchdog\n", encoding="utf-8")
        monkeypatch.setattr(autostart, "watchdog_script", lambda: script)
        return script

    def _write_plist(self, path: Path, script: Path) -> None:
        import plistlib
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "wb") as fh:
            plistlib.dump(
                {"Label": autostart.LAUNCHD_LABEL,
                 "ProgramArguments": ["/some/.venv/bin/python", str(script)]},
                fh,
            )

    # ── Reading back what is installed ───────────────────────────────────────

    def test_mac_target_is_the_script_the_plist_runs(self, flags, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        other = tmp_path / "other" / "tools" / "watchdog.py"
        self._write_plist(flags["plist"], other)
        assert autostart.entry_target() == other

    def test_mac_target_is_unknown_when_there_is_no_plist(self, flags, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        assert autostart.entry_target() is None

    def test_mac_target_is_unknown_when_the_plist_is_corrupt(self, flags, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        flags["plist"].parent.mkdir(parents=True, exist_ok=True)
        flags["plist"].write_bytes(b"not a plist")
        assert autostart.entry_target() is None

    def test_win_target_is_parsed_out_of_the_task_command(self, monkeypatch):
        """schtasks reports the whole command as one quoted string, and the
        paths contain both spaces and a drive-letter colon."""
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(autostart, "_schtasks", lambda *a: mock.Mock(
            returncode=0,
            stdout=(
                "Folder: \\\r\n"
                "TaskName:      \\FOREXTraderWatchdog\r\n"
                'Task To Run:   "C:\\FOREX Trader\\.venv\\pythonw.exe" '
                '"C:\\FOREX Trader\\tools\\watchdog.py"\r\n'
                "Scheduled Task State: Enabled\r\n"
            ),
            stderr="",
        ))
        assert autostart.entry_target() == Path(r"C:\FOREX Trader\tools\watchdog.py")

    def test_win_target_is_unknown_when_the_query_fails(self, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(autostart, "_schtasks", lambda *a: mock.Mock(
            returncode=1, stdout="", stderr="ERROR: The system cannot find the file"))
        assert autostart.entry_target() is None

    # ── The comparison ───────────────────────────────────────────────────────

    def test_an_entry_running_this_checkout_points_here(self, flags, here, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        self._write_plist(flags["plist"], here)
        assert autostart.points_at_this_checkout() is True

    def test_an_entry_running_another_checkout_does_not(self, flags, here, tmp_path, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        self._write_plist(flags["plist"], tmp_path / "other" / "tools" / "watchdog.py")
        assert autostart.points_at_this_checkout() is False

    def test_an_unreadable_entry_counts_as_pointing_here(self, flags, here, monkeypatch):
        """Unknown must not mean "wrong", or every boot on a machine whose
        plist cannot be read would reinstall the agent for no reason."""
        monkeypatch.setattr(sys, "platform", "darwin")
        assert autostart.points_at_this_checkout() is True

    # ── What startup does about it ───────────────────────────────────────────

    def test_sync_reinstalls_an_entry_that_points_at_another_checkout(
        self, flags, here, tmp_path, monkeypatch
    ):
        """The whole point: starting the app you want must make the watchdog
        supervise the app you want."""
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(autostart, "is_installed", lambda: True)
        self._write_plist(flags["plist"], tmp_path / "other" / "tools" / "watchdog.py")
        install = mock.Mock()
        monkeypatch.setattr(autostart, "_mac_install", install)

        autostart.sync_from_setting(True)

        assert install.called, "a stale entry from another checkout must be repaired"
        assert autostart.is_armed() is True

    def test_sync_leaves_an_entry_that_already_points_here_alone(
        self, flags, here, monkeypatch
    ):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(autostart, "is_installed", lambda: True)
        self._write_plist(flags["plist"], here)
        install = mock.Mock()
        monkeypatch.setattr(autostart, "_mac_install", install)

        autostart.sync_from_setting(True)

        assert not install.called, "reinstalling a correct entry on every boot is churn"
        assert autostart.is_armed() is True

    def test_sync_still_cannot_be_blocked_by_an_unreadable_entry(
        self, flags, here, monkeypatch
    ):
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(autostart, "is_installed", lambda: True)
        monkeypatch.setattr(autostart, "entry_target",
                            mock.Mock(side_effect=RuntimeError("launchctl gone")))
        autostart.sync_from_setting(True)  # must swallow
