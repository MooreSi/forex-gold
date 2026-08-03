"""Launching and recovering the MT5 bridge process (M4 B9b).

This was SimulationEngine.start_bridge_process: native-bridge reconnect,
Windows kill-and-relaunch, macOS Wine session teardown. Moved verbatim --
the only edits are the two collaborators becoming parameters, and the
repo-root lookup becoming bridge_script_path() below.

That lookup is the reason this module has a named helper instead of an
inline path expression. The original walked up from __file__ with a
hardcoded number of "..", which encodes how deeply the *calling file* is
nested. Moving the code changed that depth, and the failure would have
been silent: a missing script returns False with one warning line, so the
bridge would simply never restart. Naming it puts the depth in one place
with a test pointed straight at it.
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess

log = logging.getLogger(__name__)

# backend/src/services/broker/ -> backend/src/services -> backend/src ->
# backend -> repo root. Four levels; runtime.py needed two. Pinned by
# tests/core/test_bridge_process_relocation.py.
_REPO_ROOT = os.path.normpath(
    os.path.join(os.path.dirname(__file__), "..", "..", "..", "..")
)


def bridge_script_path() -> str:
    """Absolute path to mt5_bridge.py at the repo root."""
    return os.path.join(_REPO_ROOT, "mt5_bridge.py")


async def start_bridge_process(bridge, using_native_bridge: bool) -> bool:
    """Tear down any running bridge and start a clean new one.

    On Windows: kills the native mt5_bridge.py process and restarts it directly.
    On macOS:   tears down the entire Wine session (wineserver + all children)
                before starting a fresh instance, to avoid duplicate MT5 windows.

    With NativeMT5Bridge there's no separate process at all — recovery
    means reconnecting the in-process MT5 session instead of killing and
    relaunching a subprocess.

    Returns True if recovery was launched (not necessarily connected yet).
    """
    if using_native_bridge:
        log.info("Bridge watchdog: reconnecting in-process MT5 session (native bridge)")
        result = await bridge.reconnect()
        ok = result.get("status") == "connected"
        if not ok:
            log.warning("Bridge watchdog: native reconnect failed: %s", result.get("error"))
        return ok

    import subprocess, os as _os, sys as _sys
    from backend.src.utils import os_utils as _pu

    _bridge_py = bridge_script_path()
    if not _os.path.isfile(_bridge_py):
        log.warning("Bridge watchdog: mt5_bridge.py not found at %s", _bridge_py)
        return False

    # ── Windows: native Python execution (no Wine) ────────────────────────
    if _sys.platform == "win32":
        log.info("Bridge watchdog: stopping native bridge process")
        _pu.kill_matching("mt5_bridge.py")
        await asyncio.sleep(2)
        _pu.kill_matching("mt5_bridge.py", force=True)
        await asyncio.sleep(1)

        from backend.src.config import USER_DATA_DIR, get as _cfg_get
        from urllib.parse import urlparse as _urlparse
        _creds = str(USER_DATA_DIR / "bridge_credentials.json")
        _bridge_port = _urlparse(_cfg_get("mt5_bridge_url", "")).port or 9010
        _env = {
            **_os.environ,
            "MT5_BRIDGE_PORT":   str(_bridge_port),
            "BRIDGE_CREDS_PATH": _creds,
        }
        try:
            subprocess.Popen(
                [_sys.executable, _bridge_py],
                env=_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            log.info("Bridge watchdog: Windows native bridge subprocess launched")
            return True
        except Exception as _e:
            log.warning("Bridge watchdog: failed to start bridge: %s", _e)
            return False

    # ── macOS: full Wine session teardown then restart ─────────────────────
    import signal as _signal

    def _pids_of(pattern: str) -> list[int]:
        return _pu.pids_matching(pattern)

    def _kill_all(pids: list[int], sig: int) -> None:
        for pid in pids:
            try:
                _os.kill(pid, sig)
            except (ProcessLookupError, OSError):
                pass

    # Step 1: graceful shutdown of the entire Wine session
    log.info("Bridge restart: stopping Wine session (wineserver + all children)")
    _kill_all(_pids_of("wineserver"),    _signal.SIGTERM)
    _kill_all(_pids_of("mt5_bridge.py"), _signal.SIGTERM)
    await asyncio.sleep(3)

    # Step 2: force-kill any survivors
    for _pattern in ("wineserver", "mt5_bridge.py", "terminal64.exe", "winewrapper.exe"):
        _kill_all(_pids_of(_pattern), _signal.SIGKILL)

    await asyncio.sleep(2)

    _remaining_mt5 = _pids_of("terminal64.exe")
    if _remaining_mt5:
        log.warning(
            "Bridge restart: %d terminal64.exe process(es) still alive after kill — "
            "proceeding; may result in duplicate MT5 if CrossOver re-uses it",
            len(_remaining_mt5),
        )
    else:
        log.info("Bridge restart: clean slate — no terminal64.exe or wineserver running")

    try:
        from backend.src.config import load as _cfg_load, USER_DATA_DIR
        _cfg = _cfg_load()
        _wine = (
            _cfg.get("wine_bin")
            or "/Applications/CrossOver.app/Contents/SharedSupport/CrossOver/bin/wine"
        )
        _backend = _cfg.get("bridge_backend", "crossover")
        if _backend == "crossover":
            _bottle = _os.path.expanduser(
                "~/Library/Application Support/CrossOver/Bottles/MetaTrader 5"
            )
            _extra_env = {"CX_BOTTLE": _bottle, "CX_NO_BROWSER": "1"}
        else:
            _bottle = _os.path.expanduser(
                _cfg.get("mt5_bottle_path") or "~/.wine_mt5"
            )
            _extra_env = {}

        _bridge_win = "Z:" + _bridge_py.replace("/", "\\")
        _mac_creds  = str(USER_DATA_DIR / "bridge_credentials.json")
        _win_creds  = "Z:" + _mac_creds.replace("/", "\\")
        from urllib.parse import urlparse as _urlparse
        _bridge_port = _urlparse(_cfg.get("mt5_bridge_url", "")).port or 9010

        _env = {
            **_os.environ,
            "WINEPREFIX":        _bottle,
            "WINEDEBUG":         "-all",
            "MT5_BRIDGE_PORT":   str(_bridge_port),
            "BRIDGE_CREDS_PATH": _win_creds,
            **_extra_env,
        }
        subprocess.Popen(
            [_wine, "C:\\Python311\\python.exe", _bridge_win],
            env=_env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        log.info("Bridge watchdog: macOS Wine bridge subprocess launched")
        return True
    except Exception as _e:
        log.warning("Bridge watchdog: failed to start bridge subprocess: %s", _e)
        return False
