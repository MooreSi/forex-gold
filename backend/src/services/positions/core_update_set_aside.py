"""Put uncommitted edits in `git stash` before Update force-checkouts origin.

apply_update() ends in `checkout -f`, which resets every tracked file. On a
client install that is the point: drift goes, origin lands. On the owner's
own checkout it discarded three sessions of uncommitted work (2026-10-11,
tests/positions/test_update_keeps_local_edits.py). Stashing first keeps both:
the update still lands, and `git stash list` still has the edits.

Untracked files are left alone: `checkout -f` never touches them unless
origin adds a tracked file at the same path.
"""
from __future__ import annotations

import logging
import subprocess
import time
from pathlib import Path

from backend.src.services.positions.core_git_exe import git_exe as _git_exe

log = logging.getLogger(__name__)


def _git(root: Path, *args: str) -> tuple[int, str, str]:
    """git against `root` only. --git-dir stops git walking up to a parent
    repository when `root/.git` is not a usable one."""
    try:
        proc = subprocess.run(
            [_git_exe() or "git", f"--git-dir={root / '.git'}", f"--work-tree={root}",
             # A stash is a commit; a machine with no identity configured
             # would otherwise refuse it.
             "-c", "user.name=FOREX Update", "-c", "user.email=update@localhost", *args],
            cwd=str(root), capture_output=True, text=True, timeout=60,
        )
        return proc.returncode, proc.stdout, proc.stderr
    except Exception as e:                     # git missing, timeout, a stubbed run
        return -1, "", str(e)


def set_aside(root: Path) -> str | None:
    """Stash tracked edits under `root`. Returns None when the checkout may be
    force-updated (clean, stashed, or status unreadable -- the old behaviour),
    else why it may not."""
    rc, out, err = _git(root, "status", "--porcelain", "--untracked-files=no")
    if rc != 0 or not isinstance(out, str):
        log.warning("[Update] could not read the checkout's status, not stashing: %s",
                    (err or "").strip()[:200])
        return None
    changed = [line for line in out.splitlines() if line.strip()]
    if not changed:
        return None
    label = time.strftime("Set aside by Update %Y-%m-%d %H:%M:%S")
    rc, out, err = _git(root, "stash", "push", "-m", label)
    if rc != 0:
        return ("update stopped: %d uncommitted file(s) could not be set aside: %s"
                % (len(changed), (err.strip() or out.strip() or "git stash failed")[:200]))
    log.warning("[Update] %d uncommitted file(s) put in git stash as '%s'",
                len(changed), label)
    return None
