"""Credentials — split from core/database.py.
Extracted from forex_trader/core/database.py -- see
docs/todo/refactor/core-database-migration/. Verbatim port: same functions,
same SQL, same behavior, using database.py's own db()/to_db_thread()
machinery (unchanged, already correct -- this is a pure file-size split,
not a connection-layer migration). Re-exported from database.py so every
existing `db_module.<name>` call site works completely unchanged.
"""
import asyncio
import json
import logging
import os
import sqlite3
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

from forex_trader.core.database import db, row_to_dict, to_db_thread, _schedule_coro  # noqa: E402


# ── MT5 credentials — always stored in the demo DB (env-independent) ─────────

def _master_creds_path() -> str:
    from forex_trader.config import DATA_DIR
    return str(DATA_DIR / "forex_trader_demo.db")


_CRED_SECRET_COLS = ("password_enc", "live_password_enc")


def get_mt5_credentials() -> dict:
    """Read MT5 credentials from the permanent demo DB regardless of active env.
    Secret columns are decrypted before returning; plaintext legacy values are
    re-saved encrypted the first time they are seen."""
    import sqlite3 as _sq
    try:
        conn = _sq.connect(_master_creds_path(), check_same_thread=False)
        conn.row_factory = _sq.Row
        row = conn.execute("SELECT * FROM mt5_credentials WHERE id=1").fetchone()
        conn.close()
        creds = dict(row) if row else {}
    except Exception:
        return {}

    try:
        from forex_trader.core import secrets as _sec
        legacy = {}
        for col in _CRED_SECRET_COLS:
            val = creds.get(col) or ""
            if val and not _sec.is_encrypted(val):
                legacy[col] = val          # plaintext row — upgrade below
            creds[col] = _sec.decrypt(val)
        if legacy:
            save_mt5_credentials(legacy)   # save_mt5_credentials encrypts
    except Exception as e:
        logging.getLogger(__name__).warning("[DB] credential decrypt failed: %s", e)
    return creds


def save_mt5_credentials(updates: dict) -> None:
    """Write MT5 credentials to the permanent demo DB regardless of active env.
    Secret columns are encrypted before hitting disk."""
    import sqlite3 as _sq
    try:
        from forex_trader.core import secrets as _sec
        updates = {
            k: (_sec.encrypt(v) if k in _CRED_SECRET_COLS and v else v)
            for k, v in updates.items()
        }
    except Exception as e:
        logging.getLogger(__name__).warning("[DB] credential encrypt failed: %s", e)
    conn = _sq.connect(_master_creds_path(), check_same_thread=False)
    try:
        sc = ", ".join(f"{k}=?" for k in updates)
        conn.execute(
            f"UPDATE mt5_credentials SET {sc} WHERE id=1", list(updates.values())
        )
        conn.commit()
    finally:
        conn.close()


def _bridge_creds_path() -> str:
    """Return the macOS path to bridge_credentials.json.

    Lives in the user data directory (outside the project folder) so that
    distributing a new version of the app never ships credentials.
    The Wine bridge reads the same file via the Z: drive mount; the launcher
    script converts this path and passes it as BRIDGE_CREDS_PATH.
    """
    from forex_trader.config import USER_DATA_DIR
    return str(USER_DATA_DIR / "bridge_credentials.json")


def sync_bridge_credentials_file(env: str) -> bool:
    """
    Write the correct credentials for `env` directly to bridge_credentials.json
    so the bridge picks them up on the next (re)start.  Returns True on success.
    """
    import json
    creds = get_mt5_credentials()
    if env == "live":
        login    = int(creds.get("live_login") or 0)
        password = creds.get("live_password_enc") or ""
        server   = (creds.get("live_server") or "").strip()
    else:
        login    = int(creds.get("login") or 0)
        password = creds.get("password_enc") or ""
        server   = (creds.get("server") or "").strip()

    if not login or not password or not server:
        return False
    try:
        payload: dict = {"login": login, "password": password, "server": server}
        # Include terminal_path so the bridge can pass it to mt5.initialize() and
        # attach to the existing terminal rather than launching a new instance.
        if env == "live":
            tp = (creds.get("live_terminal_path") or "").strip()
        else:
            tp = (creds.get("terminal_path") or "").strip()
        if tp:
            payload["terminal_path"] = tp
        creds_file = _bridge_creds_path()
        with open(creds_file, "w") as f:
            json.dump(payload, f)
        # The bridge needs plaintext, but nothing else does — owner-only perms.
        try:
            os.chmod(creds_file, 0o600)
        except OSError:
            pass
        return True
    except Exception as e:
        log.warning("sync_bridge_credentials_file failed: %s", e)
        return False
