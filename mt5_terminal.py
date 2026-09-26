"""Finding the Windows MetaTrader terminal and connecting to it.

Standard library only: mt5_bridge.py imports this, and on macOS the bridge runs
under Wine's Python, which has none of the app's dependencies.

The sequence `mt5_bridge._connect` used until 2026-09-26 attached without a
path and, failing that, started the terminal from a saved path WITHOUT the
account. On the owner's Windows VPS (Vantage build, C:\\Program Files\\Vantage
Markets MT5 Terminal) the first failed every time with (1, 'Success'), no path
was saved, and once one was the terminal refused with (-6, 'Terminal:
Authorization failed'). See tests/broker/test_mt5_terminal.py.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _read_origin(origin: Path) -> str:
    """MetaTrader writes origin.txt as UTF-16 with a BOM."""
    raw = origin.read_bytes()
    text = raw.decode("utf-16") if raw[:2] in (b"\xff\xfe", b"\xfe\xff") else raw.decode("utf-8-sig")
    return text.strip()


def find_terminal_path(appdata: Optional[Path] = None) -> str:
    """terminal64.exe of the one MetaTrader installed on this machine, or "".

    Every terminal's data folder (%APPDATA%\\MetaQuotes\\Terminal\\<id>) names
    its install folder in origin.txt. Several installs is "" on purpose:
    starting the wrong one opens a second terminal, possibly on another
    account, so that case is the saved path's job.
    """
    base = Path(appdata) if appdata is not None else Path(os.environ.get("APPDATA", ""))
    found: set[str] = set()
    try:
        for origin in (base / "MetaQuotes" / "Terminal").glob("*/origin.txt"):
            try:
                exe = Path(_read_origin(origin)) / "terminal64.exe"
            except (OSError, UnicodeDecodeError):
                continue
            if exe.is_file():
                found.add(str(exe))
    except OSError:
        return ""
    return found.pop() if len(found) == 1 else ""


def connect(mt5, *, login: int, password: str, server: str, terminal_path: str,
            timeout_ms: int, log, appdata: Optional[Path] = None) -> tuple[bool, str]:
    """Attach to the running terminal; failing that, start or attach to it
    from a path, logging in within the same call. Returns (ok, error).

    Attaching without a path comes first: passing a path when a DIFFERENT
    install is running starts a second terminal. With a path, the account goes
    in the initialize call itself -- without it the Vantage terminal refuses
    the connection before mt5.login() is ever reached.
    """
    if mt5.initialize(timeout=timeout_ms):
        log.info("mt5.initialize() attached to existing MT5 instance")
        return True, ""
    first_error = mt5.last_error()
    mt5.shutdown()

    path = terminal_path or find_terminal_path(appdata)
    if not path:
        return False, (f"mt5.initialize() failed: {first_error}. No terminal path is "
                       "saved and none could be detected: set the terminal path in "
                       "Settings > MT5.")
    log.info("Connecting to MT5 via %s terminal path: %s",
             "saved" if terminal_path else "detected", path)
    if mt5.initialize(path=path, login=login, password=password, server=server,
                      timeout=timeout_ms):
        return True, ""
    return False, f"mt5.initialize() failed: {mt5.last_error()}"
