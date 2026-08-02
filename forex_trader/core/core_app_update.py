"""GitHub-based app self-update: Settings > Update page's "Update" button
and the header's flashing "Update Available" badge (2026-08-01).

Uses `git fetch`/`git rev-parse`/`git log` against the `origin` remote
(already configured to https://github.com/MooreSi/forex.git in this
checkout) to detect commits not yet in the local working tree, rather than
the GitHub REST API -- avoids API rate limits/auth entirely, and reuses
the exact same git plumbing the actual update (`git pull`) needs anyway.

apply_update() does not restart the process itself -- the caller (the
Settings > Update page / header popup) shows the result and triggers the
actual restart via platform_utils.restart_app(), the same mechanism the
header Power dialog's Restart button uses.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import sys
from pathlib import Path

log = logging.getLogger(__name__)

_REPO_ROOT = Path(__file__).parent.parent.parent  # forex_trader/core/core_app_update.py -> repo root
_BRANCH = "main"
_GITHUB_REPO_URL = "https://github.com/MooreSi/forex"


async def _run_git(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    def _sync() -> tuple[int, str, str]:
        proc = subprocess.run(
            ["git", *args], cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    return await asyncio.to_thread(_sync)


async def check_for_update() -> dict:
    """Fetch origin and compare local HEAD against origin/<branch>.

    Returns:
      {"available": bool, "local_sha": str, "remote_sha": str,
       "commits": [{"sha","short_sha","summary"}, ...], "error": str|None}
    `commits` is every commit in HEAD..origin/<branch>, newest first -- the
    change summary shown in the header popup and the Update page. Network/
    git failures return {"available": False, "error": "<message>"} rather
    than raising, so a flaky connection never breaks the page that calls
    this on a timer.
    """
    if not (_REPO_ROOT / ".git").exists():
        return {"available": False, "error": "not a git checkout"}

    rc, _, err = await _run_git("fetch", "origin", _BRANCH)
    if rc != 0:
        return {"available": False, "error": (err.strip() or "git fetch failed")[:200]}

    rc, local_sha, _ = await _run_git("rev-parse", "HEAD")
    if rc != 0:
        return {"available": False, "error": "could not resolve local HEAD"}
    local_sha = local_sha.strip()

    rc, remote_sha, _ = await _run_git("rev-parse", f"origin/{_BRANCH}")
    if rc != 0:
        return {"available": False, "error": f"could not resolve origin/{_BRANCH}"}
    remote_sha = remote_sha.strip()

    if local_sha == remote_sha:
        return {"available": False, "local_sha": local_sha, "remote_sha": remote_sha,
                "commits": [], "error": None}

    rc, log_out, _ = await _run_git(
        "log", f"{local_sha}..{remote_sha}", "--no-merges", "--pretty=format:%H%x1f%h%x1f%s",
    )
    commits = []
    if rc == 0 and log_out.strip():
        for line in log_out.strip().splitlines():
            parts = line.split("\x1f")
            if len(parts) == 3:
                commits.append({"sha": parts[0], "short_sha": parts[1], "summary": parts[2]})

    return {
        "available": True, "local_sha": local_sha, "remote_sha": remote_sha,
        "commits": commits, "error": None,
    }


async def apply_update() -> dict:
    """git fetch + hard-reset to origin/<branch>, reinstall requirements.txt
    into the venv, clear stale __pycache__ dirs -- mirrors remote/client.py's
    admin-console push-update _apply_update() pip-install/cache-clear steps,
    just sourced from git instead of a pushed zip.

    Uses `reset --hard`, not `pull --ff-only`: the admin console's own zip-
    based push (remote/client.py's _apply_update()) writes files straight to
    disk without touching git at all, which leaves the working tree "dirty"
    from git's point of view after every such push -- a ff-only pull then
    refuses ("local changes would be overwritten by merge") even though none
    of this is a real local edit worth protecting. No client machine is
    meant to carry independent commits; each one just mirrors origin, so
    discarding local drift here is the correct, expected outcome, not data
    loss. Anything a client actually needs to keep -- config.yaml, the
    licence store, .venv, the DB -- is already gitignored and untouched by
    this.

    Returns {"ok": bool, "error": str|None}. Does not restart -- call
    platform_utils.restart_app() after a successful result.
    """
    rc, out, err = await _run_git("fetch", "origin", _BRANCH)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or out.strip() or "git fetch failed")[:300]}

    rc, out, err = await _run_git("reset", "--hard", f"origin/{_BRANCH}")
    if rc != 0:
        return {"ok": False, "error": (err.strip() or out.strip() or "git reset failed")[:300]}

    def _pip_install() -> None:
        req_file = _REPO_ROOT / "requirements.txt"
        if not req_file.exists():
            return
        venv_pip = (
            _REPO_ROOT / ".venv" / "Scripts" / "pip.exe" if sys.platform == "win32"
            else _REPO_ROOT / ".venv" / "bin" / "pip"
        )
        if venv_pip.exists():
            args = [str(venv_pip), "install", "--quiet", "-r", str(req_file)]
        else:
            args = [sys.executable, "-m", "pip", "install", "--quiet", "-r", str(req_file)]
        subprocess.run(
            args, cwd=str(_REPO_ROOT), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            timeout=180, check=False,
        )

    def _clear_pycache() -> None:
        for p in _REPO_ROOT.rglob("__pycache__"):
            shutil.rmtree(p, ignore_errors=True)

    try:
        await asyncio.to_thread(_pip_install)
        await asyncio.to_thread(_clear_pycache)
    except Exception as e:
        log.warning("[Update] pip install / cache clear step failed: %s", e)
        # The pull already succeeded -- surface this as a soft warning, not
        # a hard failure, since a stale dependency/cache issue is
        # recoverable (Save & Restart again) whereas re-pulling isn't safe
        # to retry blindly.
        return {"ok": True, "error": f"update pulled, but post-update step failed: {e}"}

    return {"ok": True, "error": None}
