"""How the app comes back after a licence activation, an admin revoke, or an
admin-pushed update.

`_do_restart()` is the last thing every one of those paths calls. If it fails,
the app is simply gone: the process has already exited, the browser sits on
the "Licence Activated / Loading..." page forever, and the user has to launch
it by hand. That is exactly what happened on a fresh macOS install on
2026-09-04 -- registration was approved, the licence verified and stored, the
log ended on "Licence received -- signalling UI then restarting", and the app
never came back.

The old POSIX implementation spawned a *detached* `bash -c "sleep 3 && python
run.py"` child and then hard-exited. That relies on the child outliving its
parent's session teardown. Under Terminal.app (the double-clicked
`FOREX Start.command`) the window's shell exits the moment the app process
does, and the survival of a `setsid` grandchild spawned one second earlier is
not something the app should be betting its only route back on.

`os.execv` does not bet on anything: it replaces this process image in place.
Same PID, same session, same parent, same terminal window -- there is no child
to lose. `guard.py`'s own "Activate Manually" button has always used execv on
this platform; the automatic path just never did.

Nothing here ever restarts a process: `os.execv`, `os._exit` and
`subprocess.Popen` are stubbed in every test.
"""
from __future__ import annotations

import os
import subprocess
import sys

import pytest

from backend.src.services.cluster.remote import _update


@pytest.fixture
def spy(monkeypatch):
    """Stubs every way out of the process and records what was attempted."""
    rec: dict = {"execv": [], "chdir": [], "popen": [], "exit": []}
    monkeypatch.setattr(os, "execv", lambda path, argv: rec["execv"].append((path, list(argv))))
    monkeypatch.setattr(os, "chdir", lambda d: rec["chdir"].append(str(d)))
    monkeypatch.setattr(os, "_exit", lambda code: rec["exit"].append(code))
    monkeypatch.setattr(
        subprocess, "Popen",
        lambda *a, **kw: rec["popen"].append((list(a[0]), kw.get("cwd"))) or _FakeProc(),
    )
    return rec


class _FakeProc:
    pid = 0


class TestThePosixRestartHappensInThisProcess:
    """The fix. Old code called Popen and never touched execv."""

    def test_it_execs_run_py_instead_of_spawning_a_detached_child(self, spy, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

        _update._do_restart()

        assert spy["execv"], (
            "the POSIX restart must replace this process with a fresh run.py, "
            "not spawn a detached child and die"
        )
        interpreter, argv = spy["execv"][0]
        assert argv[0] == interpreter
        assert argv[1:] == ["run.py", "--no-browser"], argv

    def test_it_execs_from_the_checkout_root(self, spy, monkeypatch):
        """`run.py` is relative, so the cwd has to be the checkout root -- the
        detached-child version passed cwd= to Popen and this one must chdir."""
        monkeypatch.setattr(sys, "platform", "darwin")
        from backend.src.utils.os_utils import repo_root

        _update._do_restart()

        assert spy["chdir"] == [str(repo_root())]

    def test_it_uses_the_checkouts_own_venv_interpreter_when_present(self, spy, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")
        from backend.src.utils.os_utils import repo_root
        venv_python = repo_root() / ".venv" / "bin" / "python3"

        _update._do_restart()

        interpreter = spy["execv"][0][0]
        if venv_python.exists():
            assert interpreter == str(venv_python)
        else:
            assert interpreter == sys.executable


class TestAFailedExecStillGetsTheAppBack:
    """execv only returns if it failed. Falling through to nothing would leave
    the process alive but half-restarted, holding port 8888 against the very
    relaunch it is supposed to be performing."""

    def test_an_oserror_falls_back_to_the_detached_relaunch(self, spy, monkeypatch):
        monkeypatch.setattr(sys, "platform", "darwin")

        def _boom(path, argv):
            raise OSError("Exec format error")
        monkeypatch.setattr(os, "execv", _boom)

        _update._do_restart()

        assert spy["popen"], "a failed execv must still spawn the delayed relaunch"
        cmd, cwd = spy["popen"][0]
        assert "run.py" in " ".join(cmd)
        assert spy["exit"] == [0]


class TestWindowsIsUnchanged:
    """Windows relies on the bat launcher's exit-code-42 loop plus its own
    detached relaunch. execv on Windows is a spawn-and-exit emulation that
    would hand the bat loop the wrong exit code, so that path must not move."""

    def test_windows_still_spawns_and_exits_with_the_relaunch_code(self, spy, monkeypatch):
        monkeypatch.setattr(sys, "platform", "win32")
        # The creationflags constants only exist on Windows; this suite runs
        # on POSIX too, so supply them rather than skipping the assertion.
        for _flag in ("DETACHED_PROCESS", "CREATE_NEW_PROCESS_GROUP",
                      "CREATE_BREAKAWAY_FROM_JOB"):
            if not hasattr(subprocess, _flag):
                monkeypatch.setattr(subprocess, _flag, 0, raising=False)

        _update._do_restart()

        assert spy["execv"] == []
        assert spy["popen"], "windows must still spawn the delayed relaunch child"
        assert spy["exit"] == [_update._RESTART_EXIT_CODE]
        assert spy["chdir"] == []
