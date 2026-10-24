"""The app installs its own git on Windows when the machine has none.

Both of our Windows routes put PortableGit in %LOCALAPPDATA%\\Programs\\PortableGit:
the installer always, "Setup & Start FOREX.bat" whenever git is missing. A
third-party repackaged zip ran neither (2026-10-23, 10_0_0_3: the app ran from
"FOREX_Trader_WindowsServer_含环境_20261018\\.runtime\\Python313"), so the
machine had no git and every admin-console push failed with "git not found on
PATH". We do not build that zip, so the app makes sure of git itself, however
its files got onto the machine. Spec 027.

Same build, same folder, as the .bat and the installer, so `core_git_exe`
finds it without a restart. The download is checked against a pinned SHA-256
before it is run: this is the app fetching and executing a program on its own.
The pin lives here, in the .bat and in the .iss, and moves in all three
together (rules/50-workflow.md §7); tests/positions/test_git_self_install.py
fails when they disagree.

Not macOS: there git comes from Homebrew or the Command Line Tools, and
`FOREX Start.command` already sees to that.
"""
from __future__ import annotations

import hashlib
import logging
import os
import subprocess
import sys
import urllib.request
from pathlib import Path

from backend.src.services.positions import core_git_exe

log = logging.getLogger(__name__)

PORTABLE_GIT_URL = (
    "https://github.com/git-for-windows/git/releases/download/"
    "v2.55.0.windows.3/PortableGit-2.55.0.3-64-bit.7z.exe"
)
#: From the release's own asset digest and its release-notes table, which agree.
PORTABLE_GIT_SHA256 = "ab00566336b5472120f9a52d34f2e79c5406535792acb0548001ffd0bd090e5d"

_DOWNLOAD_TIMEOUT_S = 120
_EXTRACT_TIMEOUT_S = 600


def _is_windows() -> bool:
    return sys.platform == "win32"


def _download(url: str, dest: Path) -> None:
    with urllib.request.urlopen(url, timeout=_DOWNLOAD_TIMEOUT_S) as resp, open(dest, "wb") as out:
        while chunk := resp.read(1 << 20):
            out.write(chunk)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1 << 20):
            digest.update(chunk)
    return digest.hexdigest()


def _extract(archive: Path, target: Path) -> None:
    """Run the 7-Zip self-extractor silently into `target`, as the .bat does."""
    proc = subprocess.run(
        [str(archive), "-y", f"-o{target}"], capture_output=True, timeout=_EXTRACT_TIMEOUT_S,
    )
    if proc.returncode != 0:
        raise RuntimeError(f"PortableGit extractor exited {proc.returncode}")


def _forget_git_version() -> None:
    # core_app_update remembers `git --version` for the life of the process,
    # including a miss, so without this the heartbeat would keep reporting no
    # git until the next restart.
    from backend.src.services.positions import core_app_update
    core_app_update._GIT_VERSION = None


def ensure_git() -> dict:
    """Install PortableGit if this Windows machine has no git.

    Returns {"installed": bool, "reason": str}; reason is one of "not-windows",
    "already-present", "no-localappdata", "download-failed", "hash-mismatch",
    "extract-failed", "installed". Never raises. Blocking: call it off the loop.
    """
    if not _is_windows():
        return {"installed": False, "reason": "not-windows"}
    if core_git_exe.git_exe():
        return {"installed": False, "reason": "already-present"}
    local = os.environ.get("LOCALAPPDATA")
    if not local:
        return {"installed": False, "reason": "no-localappdata"}

    programs = Path(local) / "Programs"
    target = programs / "PortableGit"
    archive = programs / "PortableGit-download.7z.exe"
    try:
        programs.mkdir(parents=True, exist_ok=True)
        try:
            _download(PORTABLE_GIT_URL, archive)
        except Exception as e:
            log.warning("[Git] no git on this machine and the download failed: %s", e)
            return {"installed": False, "reason": "download-failed"}
        actual = _sha256(archive)
        if actual != PORTABLE_GIT_SHA256:
            log.warning("[Git] downloaded PortableGit has the wrong SHA-256 (%s), not running it",
                        actual)
            return {"installed": False, "reason": "hash-mismatch"}
        try:
            _extract(archive, target)
        except Exception as e:
            log.warning("[Git] PortableGit could not be extracted: %s", e)
            return {"installed": False, "reason": "extract-failed"}
    finally:
        try:
            archive.unlink(missing_ok=True)
        except OSError as e:
            log.debug("[Git] could not remove %s: %s", archive, e)

    if not core_git_exe.git_exe():
        log.warning("[Git] PortableGit extracted but no git.exe under %s", target)
        return {"installed": False, "reason": "extract-failed"}
    _forget_git_version()
    log.info("[Git] installed PortableGit into %s, so this install can update itself", target)
    return {"installed": True, "reason": "installed"}
