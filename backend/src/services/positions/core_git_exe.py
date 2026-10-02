"""Which git the updater runs.

The Windows installer puts PortableGit in %LOCALAPPDATA%\\Programs\\PortableGit
but not on the machine's PATH; only the launcher .bat adds it, for the session
it starts. An app restarted any other way saw no git at all, so a broken
leftover .git was never rebuilt and the admin console said "commit unreadable"
for good (2026-10-02, a client's machine).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Optional


def git_exe() -> Optional[str]:
    """The git on PATH, else the installer's PortableGit, else None."""
    found = shutil.which("git")
    if found:
        return found
    local = os.environ.get("LOCALAPPDATA")
    if local:
        for sub in ("cmd", "bin"):
            exe = Path(local) / "Programs" / "PortableGit" / sub / "git.exe"
            if exe.is_file():
                return str(exe)
    return None
