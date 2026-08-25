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
    monkeypatch.setattr(autostart, "ARMED_FLAG", armed)
    monkeypatch.setattr(autostart, "LAST_LAUNCH_FILE", last)
    return {"armed": armed, "last": last}


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
