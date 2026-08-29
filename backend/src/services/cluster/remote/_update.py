"""Restarting the app, and applying a git update to it.

Moved verbatim out of `client.py` to keep that file inside its size budget.
Same functions, same bodies, same names; `client.py` imports them back, so
every call site is unchanged.

Nothing here is about the admin websocket -- it is the machinery the update
command drives once the decision to update has been made. Keeping it separate
means the connection loop reads as a connection loop.

`_do_restart` never returns: it exits the process with a code the launcher
watches for.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)


# ── Restart helper ───────────────────────────────────────────────────────────

_RESTART_EXIT_CODE = 42  # bat file re-runs run.py when it sees this code


def _refresh_desktop_icon(app_root: Path) -> None:
    """Re-stamp the desktop shortcut with the gold bag icon after an update.

    Uses PowerShell's WScript.Shell COM object — no extra dependencies.
    Silently skips if the shortcut doesn't exist (e.g. user declined it at
    install time) or if PowerShell fails for any reason.
    """
    # frontend/static since the refactor; was forex_trader/ui/static.
    ico_path = app_root / "frontend" / "static" / "gold_bag.ico"
    if not ico_path.exists():
        return
    bat_path = app_root / "Setup & Start FOREX.bat"
    # Check both per-user and public desktop locations
    import os
    candidates = []
    user_desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop" / "FOREX Trader.lnk"
    public_desktop = Path(os.environ.get("PUBLIC", "C:\\Users\\Public")) / "Desktop" / "FOREX Trader.lnk"
    for lnk in (user_desktop, public_desktop):
        if lnk.exists():
            candidates.append(lnk)
    if not candidates:
        log.debug("[RemoteClient] No desktop shortcut found — icon refresh skipped")
        return
    ps_lines = []
    ps_lines.append("$ws = New-Object -ComObject WScript.Shell")
    for lnk in candidates:
        ps_lines.append(f'$lnk = $ws.CreateShortcut("{lnk}")')
        ps_lines.append(f'$lnk.TargetPath = "{bat_path}"')
        ps_lines.append(f'$lnk.WorkingDirectory = "{app_root}"')
        ps_lines.append(f'$lnk.IconLocation = "{ico_path},0"')
        ps_lines.append("$lnk.Save()")
    # Force Windows to rebuild its icon cache so the new icon appears immediately
    # without the user having to log out or run ie4uinit manually.
    ps_lines += [
        "ie4uinit.exe -show",
        "Stop-Process -Name explorer -Force -ErrorAction SilentlyContinue",
        "Start-Process explorer",
    ]
    ps_script = "; ".join(ps_lines)
    try:
        subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
            timeout=15,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        log.info("[RemoteClient] Desktop shortcut icon refreshed (%d shortcut(s))", len(candidates))
    except Exception as _e:
        log.debug("[RemoteClient] Icon refresh failed (non-fatal): %s", _e)


def _do_restart() -> None:
    """Hard-exit this process so the app comes back up cleanly.

    On Windows we used to rely solely on the bat launcher's loop catching
    _RESTART_EXIT_CODE — but that only works if the app was actually started
    via the bat file. Any other launch method (double-clicked exe, Task
    Scheduler, IDE run, manual `python run.py`) left the process dead with
    no relaunch, stranding the browser on the "Licence Activated / Loading.."
    page forever. We now ALSO spawn our own detached relaunch subprocess
    (same mechanism as macOS/Linux) so the restart is self-sufficient
    regardless of how the app was launched; the bat loop, if present, simply
    sees the process exit and skips straight to its own relaunch — harmless
    overlap, not a conflict, since the new process binds the port either way.
    """
    # Marker-based: three parents landed in backend/src/services, so the
    # licence-activation restart relaunched a run.py that is not there.
    from backend.src.utils.os_utils import repo_root as _repo_root
    app_root = _repo_root()
    from backend.src.utils.os_utils import delayed_relaunch_cmd, open_restart_log
    from backend.src.config import USER_DATA_DIR as _udata
    log_path = Path(_udata) / "data" / "restart.log"

    if sys.platform == "win32":
        venv_python = app_root / ".venv" / "Scripts" / "python.exe"
        python = str(venv_python) if venv_python.exists() else sys.executable
        with open_restart_log(log_path) as _log:
            # CREATE_BREAKAWAY_FROM_JOB is required: Task Scheduler puts this
            # process (and every child it spawns) in a Job Object with
            # kill-on-close semantics, so without breakaway this relaunch
            # child dies together with this process before its delay timer
            # even elapses -- confirmed empirically, see the matching comment
            # in platform_utils.restart_app().
            subprocess.Popen(
                delayed_relaunch_cmd(python, "run.py", delay_secs=3, extra_args=["--no-browser"]),
                cwd=str(app_root),
                creationflags=(
                    subprocess.DETACHED_PROCESS
                    | subprocess.CREATE_NEW_PROCESS_GROUP
                    | subprocess.CREATE_BREAKAWAY_FROM_JOB
                ),
                stdin=subprocess.DEVNULL,
                stdout=_log,
                stderr=_log,
            )
        # Still exit with _RESTART_EXIT_CODE so a bat-file launcher (if
        # present) recognises this as a clean restart rather than a crash.
        os._exit(_RESTART_EXIT_CODE)

    # macOS / Linux path: spawn delayed relaunch, then hard-exit.
    venv_python = app_root / ".venv" / "bin" / "python3"
    python = str(venv_python) if venv_python.exists() else sys.executable
    with open_restart_log(log_path) as _log:
        subprocess.Popen(
            delayed_relaunch_cmd(python, "run.py", delay_secs=3, extra_args=["--no-browser"]),
            cwd=str(app_root),
            start_new_session=True,
            stdin=subprocess.DEVNULL,
            stdout=_log,
            stderr=_log,
        )
    os._exit(0)


# ── Update application ────────────────────────────────────────────────────────

async def _apply_git_update() -> None:
    """Handle MSG_GIT_UPDATE: run core_app_update.apply_update() (git fetch +
    force-checkout + pip install + pycache clear) and restart on success.

    Deliberately a thin wrapper, not a second implementation -- the whole
    point of the admin console's Update button sending this message instead
    of streaming a zip (as it used to) is that both the admin-triggered
    update and the client's own Settings > Update button converge on this
    one code path, so they can never drift out of sync with each other the
    way the old zip-based push and the git-based self-update used to (a zip
    push wrote files straight to disk with no git awareness at all, leaving
    the working tree "dirty" and breaking the next git-based update).
    """
    from backend.src.services.positions import core_app_update

    log.info("[RemoteClient] Git update triggered by admin — applying")
    result = await core_app_update.apply_update()
    if not result.get("ok"):
        log.error("[RemoteClient] Git update failed: %s", result.get("error"))
        return

    log.info("[RemoteClient] Update applied — restarting")
    if sys.platform == "win32":
        from backend.src.utils.os_utils import repo_root as _repo_root2
        _refresh_desktop_icon(_repo_root2())
        # Load the just-deployed client.py from disk so this push's restart
        # logic (exit code 42) takes effect immediately rather than only on
        # the next push.  Without this, _do_restart() in memory is always
        # one version behind the files we just wrote.
        try:
            import importlib.util as _ilu
            _fresh_path = Path(__file__).parent / "client.py"
            _spec = _ilu.spec_from_file_location("_client_fresh", str(_fresh_path))
            _mod  = _ilu.module_from_spec(_spec)
            _spec.loader.exec_module(_mod)
            _mod._do_restart()
        except Exception as _load_err:
            log.warning("[RemoteClient] Fresh restart load failed (%s) — fallback", _load_err)
            _do_restart()
    else:
        _do_restart()


# ── Main connection loop ──────────────────────────────────────────────────────
