"""
Cross-platform OS utilities.

All Mac-specific or Windows-specific process/network/sleep operations are
centralised here so the calling code stays platform-agnostic.  Every function
has a macOS path and a Windows path; the correct one is chosen at runtime via
sys.platform checks.
"""

import logging
import os
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


# ── Port detection ─────────────────────────────────────────────────────────────

def pids_listening_on(port: int) -> list[int]:
    """Return PIDs of all processes currently listening (LISTEN state) on `port`."""
    if sys.platform == "win32":
        try:
            out = subprocess.check_output(
                ["netstat", "-ano"],
                stderr=subprocess.DEVNULL,
            ).decode("utf-8", errors="replace")
            pids: set[int] = set()
            for line in out.splitlines():
                if f":{port} " in line and "LISTENING" in line:
                    parts = line.split()
                    if parts:
                        try:
                            pids.add(int(parts[-1]))
                        except ValueError:
                            pass
            return list(pids)
        except Exception:
            return []
    else:
        try:
            out = subprocess.check_output(
                ["lsof", f"-ti:{port}"],
                stderr=subprocess.DEVNULL,
            ).decode().strip()
            return [int(p) for p in out.split() if p]
        except Exception:
            return []


def is_port_in_use(port: int) -> bool:
    """Return True if something is currently listening on `port`."""
    return bool(pids_listening_on(port))


def is_port_listening(port: int) -> bool:
    """Strict LISTEN-state check (ignores CLOSE_WAIT client sockets)."""
    if sys.platform == "win32":
        return is_port_in_use(port)
    else:
        try:
            out = subprocess.check_output(
                ["lsof", f"-ti4TCP:{port}", "-sTCP:LISTEN"],
                stderr=subprocess.DEVNULL,
            ).strip()
            return bool(out)
        except Exception:
            return False


def free_port(port: int) -> None:
    """Kill all processes listening on `port`."""
    for pid in pids_listening_on(port):
        kill_pid(pid, force=True)


# ── Process management ─────────────────────────────────────────────────────────

def kill_pid(pid: int, force: bool = False) -> None:
    """Terminate a process by PID."""
    if sys.platform == "win32":
        args = ["taskkill", "/PID", str(pid)]
        if force:
            args.append("/F")
        subprocess.run(args, capture_output=True)
    else:
        import signal as _signal
        try:
            os.kill(pid, _signal.SIGKILL if force else _signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass


def _pids_windows_powershell(pattern: str) -> "list[int] | None":
    """Ask CIM for processes whose command line contains `pattern`.

    Returns None if PowerShell itself could not answer -- which is different
    from an empty list, and the caller falls back only on the former.

    Preferred over wmic because wmic is deprecated and absent from recent
    Windows builds; CIM is present on every supported version. Verified on
    Windows CI by tests/utils/test_process_discovery.py, which is the only
    place this repo can test it -- it is developed on a Mac.
    """
    if "'" in pattern:
        # The pattern is interpolated into a single-quoted PowerShell string.
        # Every real caller passes a literal ("wineserver", "mt5_bridge.py"),
        # so refuse rather than build a quoted string that means something
        # other than what was asked.
        log.warning("[os] Refusing a process pattern containing a quote: %r", pattern)
        return None
    # $_.ProcessId -ne $PID excludes THIS PowerShell process, and it is not
    # optional: the pattern is embedded in the command line of the very query
    # being run, so Get-CimInstance finds itself and every lookup returns one
    # spurious pid. Caught on Windows CI 2026-09-02 by the negative control --
    # a nonsense pattern that should match nothing returned a pid. kill_matching
    # kills what this returns, so a self-match means the watchdog terminates its
    # own helper and believes a dead bridge is alive.
    script = (
        "Get-CimInstance Win32_Process | "
        f"Where-Object {{ $_.ProcessId -ne $PID -and $_.CommandLine -like '*{pattern}*' }} | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=30,
        )
    except Exception as exc:
        log.warning("[os] PowerShell process lookup could not run (%s: %s)",
                    type(exc).__name__, exc)
        return None
    if proc.returncode != 0:
        log.warning("[os] PowerShell process lookup failed (rc=%s): %s",
                    proc.returncode, (proc.stderr or "").strip()[:200])
        return None
    pids = []
    for line in proc.stdout.splitlines():
        line = line.strip()
        if line.isdigit():
            pids.append(int(line))
    return pids


def _pids_windows_wmic(pattern: str) -> "list[int] | None":
    """The old mechanism, kept as a fallback for boxes without PowerShell.

    wmic is deprecated and has been removed from recent Windows builds, so
    this is no longer the primary path.
    """
    try:
        out = subprocess.check_output(
            [
                "wmic", "process", "where",
                f"CommandLine like '%{pattern}%'",
                "get", "ProcessId", "/value",
            ],
            stderr=subprocess.DEVNULL,
        ).decode("utf-8", errors="replace")
    except subprocess.CalledProcessError:
        return []          # ordinary "no matches"
    except Exception as exc:
        log.warning("[os] wmic process lookup could not run (%s: %s)",
                    type(exc).__name__, exc)
        return None
    pids = []
    for line in out.splitlines():
        line = line.strip()
        if line.lower().startswith("processid="):
            try:
                pids.append(int(line.split("=", 1)[1]))
            except ValueError:
                pass
    return pids


def pids_matching(pattern: str) -> list[int]:
    """Return PIDs of all processes whose command line contains `pattern`.

    An empty list means "nothing matched". It must never mean "the lookup
    broke": the bridge watchdog kills what this returns, so a silent [] makes
    a dead bridge look like a healthy one with nothing to restart. Every
    failure path here logs.
    """
    if sys.platform == "win32":
        pids = _pids_windows_powershell(pattern)
        if pids is not None:
            return pids
        pids = _pids_windows_wmic(pattern)
        if pids is not None:
            return pids
        log.error("[os] No working process lookup on this machine — neither "
                  "PowerShell nor wmic could answer for %r. The bridge "
                  "watchdog cannot see processes and will not restart a dead "
                  "bridge.", pattern)
        return []
    try:
        out = subprocess.check_output(
            ["pgrep", "-f", pattern], stderr=subprocess.DEVNULL
        ).decode().strip().split()
        return [int(p) for p in out if p]
    except subprocess.CalledProcessError:
        return []          # pgrep exits 1 when nothing matches
    except Exception as exc:
        # Previously uncaught: a missing pgrep raised FileNotFoundError
        # straight out of the watchdog instead of being handled.
        log.warning("[os] Could not look up processes matching %r (%s: %s)"
                    " — treating as no matches, but the lookup itself "
                    "failed.", pattern, type(exc).__name__, exc)
        return []


def kill_matching(pattern: str, force: bool = False) -> int:
    """Kill all processes whose command line matches `pattern`. Returns count killed."""
    pids = pids_matching(pattern)
    for pid in pids:
        kill_pid(pid, force=force)
    return len(pids)


# ── Sleep prevention ───────────────────────────────────────────────────────────

class _WindowsSleepGuard:
    """Wraps SetThreadExecutionState so callers get the same interface as a Popen."""

    _ES_CONTINUOUS       = 0x80000000
    _ES_SYSTEM_REQUIRED  = 0x00000001

    def __init__(self):
        self._active = False

    def start(self) -> None:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(
            self._ES_CONTINUOUS | self._ES_SYSTEM_REQUIRED
        )
        self._active = True

    def stop(self) -> None:
        import ctypes
        ctypes.windll.kernel32.SetThreadExecutionState(self._ES_CONTINUOUS)
        self._active = False

    # Duck-type to match subprocess.Popen so callers can treat both uniformly
    def poll(self) -> None:
        return None if self._active else 0

    def terminate(self) -> None:
        self.stop()


_win_sleep_guard: "_WindowsSleepGuard | None" = (
    _WindowsSleepGuard() if sys.platform == "win32" else None
)


def start_prevent_sleep():
    """
    Prevent the OS from sleeping.  Returns an opaque handle.
    Pass the handle to stop_prevent_sleep() when done.
    """
    if sys.platform == "win32":
        _win_sleep_guard.start()
        return _win_sleep_guard
    elif sys.platform == "darwin":
        try:
            return subprocess.Popen(
                ["caffeinate", "-i", "-w", str(os.getpid())],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            return None
    return None


def stop_prevent_sleep(handle) -> None:
    """Cancel sleep prevention started by start_prevent_sleep()."""
    if handle is None:
        return
    if hasattr(handle, "terminate"):
        handle.terminate()


def is_preventing_sleep(handle) -> bool:
    """Return True if the sleep-prevention handle is still active."""
    if handle is None:
        return False
    if hasattr(handle, "poll"):
        return handle.poll() is None
    return False


# ── Subprocess restart helper ──────────────────────────────────────────────────

def open_restart_log(path, max_bytes: int = 10 * 1024 * 1024, backup_count: int = 3):
    """
    Open restart.log for appending, rotating it first if it exceeds max_bytes.
    Keeps up to backup_count numbered backups (restart.log.1, restart.log.2, ...).
    Returns a standard file object suitable for use as subprocess stdout/stderr.
    """
    from pathlib import Path
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and path.stat().st_size >= max_bytes:
        for i in range(backup_count - 1, 0, -1):
            src = path.parent / f"{path.name}.{i}"
            dst = path.parent / f"{path.name}.{i + 1}"
            if src.exists():
                src.replace(dst)
        path.replace(path.parent / f"{path.name}.1")
    return open(path, "a", encoding="utf-8", errors="replace")


def delayed_relaunch_cmd(
    python: str,
    script: str,
    delay_secs: int = 5,
    extra_args: list[str] | None = None,
) -> list[str]:
    """
    Return a command list that waits `delay_secs` then relaunches `script` with
    `python`.  Used by the in-app restart function; works on both platforms.
    `extra_args` are appended verbatim after the script name.

    Windows note: `python`/`script` must be passed as *separate* argv elements,
    not pre-wrapped in manual quotes inside a single string element — Popen's
    list2cmdline() quotes each element itself when it contains spaces (as any
    install path with a space, e.g. "FOREX Trader", does), and pre-quoting
    on top of that produces a doubly-escaped `\"...\"` command line that
    cmd.exe can't parse (surfaced as "... is not recognized as an internal
    or external command").
    """
    extra = extra_args or []
    if sys.platform == "win32":
        return [
            "cmd", "/c",
            "timeout", "/t", str(delay_secs), "/nobreak", ">nul",
            "&&", python, script, *extra,
        ]
    else:
        args_str = " ".join(extra_args) if extra_args else ""
        args_part = f" {args_str}" if args_str else ""
        return ["bash", "-c", f"sleep {delay_secs} && '{python}' '{script}'{args_part}"]


def app_python(root) -> str:
    """Return the interpreter that should be used to launch run.py.

    Prefers the checkout's own .venv (a symlink is fine -- it resolves through
    normally), falling back to whatever interpreter is running right now. Shared
    by restart_app() and the autostart watchdog so a relaunch never picks a
    different Python than the one the app was installed against.
    """
    from pathlib import Path
    root = Path(root)
    if sys.platform == "win32":
        venv_python = root / ".venv" / "Scripts" / "python.exe"
    else:
        venv_python = root / ".venv" / "bin" / "python3"
    return str(venv_python) if venv_python.exists() else sys.executable


def restart_app(root) -> None:
    """Spawn a detached relaunch of run.py after a delay, then shut this
    process down -- the shared restart mechanism used by both the header
    Power dialog and the Settings > Update "apply update" flow
    (core_app_update.py). Caller is responsible for notifying the user and
    for calling this from a context where `nicegui.app.shutdown()` makes
    sense (a running NiceGUI server); this function performs the shutdown
    itself as its final step.
    """
    from pathlib import Path
    root = Path(root)
    python = app_python(root)

    from backend.src.config import USER_DATA_DIR
    log_path = USER_DATA_DIR / "data" / "restart.log"
    cmd = delayed_relaunch_cmd(python, "run.py", delay_secs=5, extra_args=["--no-browser"])
    with open_restart_log(log_path) as _restart_log:
        if sys.platform == "win32":
            # CREATE_BREAKAWAY_FROM_JOB is required, not optional: this app is
            # normally launched by Task Scheduler, which places it (and every
            # child it spawns) in a Job Object with kill-on-close semantics.
            # DETACHED_PROCESS/CREATE_NEW_PROCESS_GROUP alone only detach the
            # console/signal group, not job membership -- without breakaway,
            # this relaunch child dies the instant this process exits below,
            # before its delay timer even elapses (confirmed empirically: the
            # child never survived without this flag). See _do_restart() in
            # remote/client.py for the same fix on the licence-activation and
            # revocation restart paths.
            subprocess.Popen(
                cmd,
                cwd=str(root),
                creationflags=(
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_BREAKAWAY_FROM_JOB
                ),
                stdin=subprocess.DEVNULL,
                stdout=_restart_log,
                stderr=_restart_log,
            )
        else:
            subprocess.Popen(
                cmd,
                cwd=str(root),
                start_new_session=True,
                stdin=subprocess.DEVNULL,
                stdout=_restart_log,
                stderr=_restart_log,
            )

    shutdown_ui()


def shutdown_ui() -> bool:
    """Ask the running NiceGUI server to stop. Returns False if it could not.

    The one place in the backend that knows how the UI is stopped.
    `no-nicegui-in-the-backend` is a counted contract -- the backend must be
    runnable, testable and schedulable without a UI framework present -- and
    this used to be done here AND again by hand in
    services/telegram/bot_infra for /restartapp, which put the count over its
    baseline for a call that is identical in both places.

    Never raises. Callers are mid-restart or mid-update with the relaunch
    subprocess already spawned; an exception here would abort that and leave
    nothing running at all. A headless instance, or one whose server has
    already stopped, is a False rather than a failure.
    """
    try:
        from nicegui import app
        app.shutdown()
        return True
    except Exception as exc:
        log.warning("UI shutdown failed (relaunch, if any, is unaffected): %s", exc)
        return False


def repo_root() -> Path:
    """The checkout root -- the directory holding run.py.

    Walks up for the marker instead of counting parents. The 2026-08-25
    upstream merge found four modules whose fixed parent counts were correct
    from their pre-refactor homes and silently wrong from their new ones
    (remote/client.py and remote/server.py resolved VERSION and CHANGELOG.md
    to backend/, mt5_native.py looked for mt5_bridge.py in backend/src/, and
    ea_bridge/core_autostart the same) -- so "which commit is this client
    running" answered "unknown" and the Wine bridge could not be found.
    Counting parents breaks on the next move; the marker does not.
    """
    here = Path(__file__).resolve()
    for candidate in here.parents:
        if (candidate / "run.py").exists():
            return candidate
    return here.parents[3]


def mask_account(value) -> str:
    """An account/login number with only its last 3 digits shown.

    The diagnostics feature uploads ~3,000 raw log lines to the admin server,
    and the MT5 connect line put the login, the broker server and the balance
    into every one of them -- confirmed from Simon's own captured logs, and
    recorded as Q005 #1 in docs/simon-handover/005-fact-finding.md. Enough tail
    is kept to tell two accounts apart in a support conversation; not enough to
    be the account number.
    """
    text = str(value or "")
    return ("*" * max(0, len(text) - 3)) + text[-3:] if len(text) > 3 else "***"


def mask_email(value) -> str:
    """An address with the local part hidden: ``***@outlook.com``."""
    text = str(value or "")
    if "@" not in text:
        return "***" if text else ""
    return "***@" + text.split("@", 1)[1]
