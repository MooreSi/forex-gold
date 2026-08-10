"""Backups for the live-money database.

Review data H4: the books were one SQLite file on one disk, with no backup of
any kind. This takes a consistent snapshot via SQLite's online backup API (safe
against a live, in-use database), keeps at most one per calendar day, and
rotates to a fixed number of copies. Destination is a local `backups/` folder
next to the DB (owner decision Q001 #4: daily local snapshot, keep 30).

Pure functions over explicit paths — no global state, no scheduler — so the
caller (app startup) decides when, and tests drive them directly.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_KEEP = 30
_PREFIX = "backup_"


def backup_now(db_path, backups_dir, *, stamp: str | None = None) -> Path:
    """Snapshot the DB at db_path into backups_dir, returning the new file path.

    Uses Connection.backup, which reads a transactionally consistent copy even
    while the app is writing — no need to stop the engines.
    """
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    stamp = stamp or datetime.now().strftime("%Y%m%d_%H%M%S")
    dest = backups_dir / f"{_PREFIX}{stamp}.db"
    src = sqlite3.connect(str(db_path))
    try:
        dst = sqlite3.connect(str(dest))
        try:
            src.backup(dst)
        finally:
            dst.close()
    finally:
        src.close()
    return dest


def rotate(backups_dir, keep: int = DEFAULT_KEEP) -> list[Path]:
    """Delete all but the `keep` newest backups. Returns what was removed."""
    backups_dir = Path(backups_dir)
    files = sorted(backups_dir.glob(f"{_PREFIX}*.db"), key=lambda p: p.stat().st_mtime)
    remove = files[:-keep] if keep >= 0 and len(files) > keep else []
    for p in remove:
        try:
            p.unlink()
        except OSError as e:
            log.warning("could not remove old backup %s: %s", p, e)
    return remove


def _has_backup_today(backups_dir: Path) -> bool:
    today = datetime.now().date()
    for p in backups_dir.glob(f"{_PREFIX}*.db"):
        if datetime.fromtimestamp(p.stat().st_mtime).date() == today:
            return True
    return False


def maybe_daily_backup(db_path, backups_dir, keep: int = DEFAULT_KEEP) -> Path | None:
    """Take at most one backup per calendar day, then rotate.

    Returns the new backup's path, or None if today's already exists. Intended
    to be called once at app startup: an app that runs on any given day gets a
    snapshot that day, with no background scheduler to keep alive.
    """
    backups_dir = Path(backups_dir)
    backups_dir.mkdir(parents=True, exist_ok=True)
    if _has_backup_today(backups_dir):
        return None
    dest = backup_now(db_path, backups_dir)
    rotate(backups_dir, keep)
    return dest
