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
from typing import Optional
from backend.src.services.ai import provider as ai_provider
from backend.src.utils.os_utils import repo_root as _repo_root

log = logging.getLogger(__name__)

_REPO_ROOT = _repo_root()  # walks up for run.py -- see os_utils.repo_root()
_BRANCH = "main"
_GITHUB_REPO_URL = "https://github.com/MooreSi/forex"


async def _run_git(*args: str, timeout: float = 30.0) -> tuple[int, str, str]:
    def _sync() -> tuple[int, str, str]:
        proc = subprocess.run(
            ["git", *args], cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=timeout,
        )
        return proc.returncode, proc.stdout, proc.stderr
    return await asyncio.to_thread(_sync)


# Why get_commit_report() couldn't produce a SHA. Sent verbatim to the admin
# console (remote/client.py -> remote/server.py) so its Remote Clients card can
# say what's actually wrong with a client instead of guessing.
COMMIT_NO_CHECKOUT   = "no-checkout"    # never updated: install scripts copy files, never clone
COMMIT_GIT_UNREADABLE = "git-unreadable"  # .git is there but neither git nor a direct read worked


def _restart() -> None:
    """Relaunch the app -- the same mechanism the header's Power dialog uses.
    Spawns a detached run.py after a delay, then asks the running NiceGUI
    server to shut down (graceful; not a hard os._exit -- see
    os_utils.restart_app's own docstring). A module-level seam so tests can
    stub it instead of actually spawning a process."""
    from backend.src.utils.os_utils import restart_app
    restart_app(_REPO_ROOT)


def _is_sha(value: str) -> bool:
    return len(value) == 40 and all(c in "0123456789abcdef" for c in value.lower())


def _read_sha_from_git_dir() -> str:
    """HEAD's SHA read straight off .git, with no `git` process involved.

    The install scripts only ever put a working `git` on the machine when
    Homebrew is present ("FOREX Start.command"), and on macOS an Xcode
    Command Line Tools stub or a "dubious ownership" refusal makes every
    `git` invocation exit non-zero even in a perfectly good checkout. Reading
    the ref files is enough to answer "which commit is this?", so commit
    reporting shouldn't be lost to any of that.

    Returns the full 40-char SHA, or "" if anything about the layout is
    unexpected -- this is a best-effort fallback, never a hard requirement.
    """
    git_path = _REPO_ROOT / ".git"
    try:
        if git_path.is_file():
            # Linked worktree / submodule: ".git" is a file "gitdir: <path>".
            text = git_path.read_text(encoding="utf-8").strip()
            if not text.startswith("gitdir:"):
                return ""
            git_dir = Path(text.split(":", 1)[1].strip())
            if not git_dir.is_absolute():
                git_dir = (_REPO_ROOT / git_dir).resolve()
        else:
            git_dir = git_path

        head = (git_dir / "HEAD").read_text(encoding="utf-8").strip()
        if not head.startswith("ref:"):
            return head if _is_sha(head) else ""
        ref = head.split(":", 1)[1].strip()

        # A linked worktree keeps its own HEAD but shares refs with the main
        # repo, pointed at by its "commondir" file.
        search_dirs = [git_dir]
        commondir_file = git_dir / "commondir"
        if commondir_file.exists():
            common = Path(commondir_file.read_text(encoding="utf-8").strip())
            if not common.is_absolute():
                common = (git_dir / common).resolve()
            search_dirs.append(common)

        for d in search_dirs:
            loose = d / ref
            if loose.is_file():
                sha = loose.read_text(encoding="utf-8").strip()
                if _is_sha(sha):
                    return sha
        for d in search_dirs:
            packed = d / "packed-refs"
            if not packed.is_file():
                continue
            for line in packed.read_text(encoding="utf-8").splitlines():
                if line.startswith("#") or line.startswith("^"):
                    continue
                parts = line.split(" ", 1)
                if len(parts) == 2 and parts[1].strip() == ref and _is_sha(parts[0]):
                    return parts[0].strip()
    except Exception:
        return ""
    return ""


def get_commit_report(short: bool = True) -> tuple[str, str]:
    """(sha, reason) for this install's HEAD commit, with no network call
    (unlike check_for_update(), which does a `git fetch`) -- safe to call
    cheaply and often, e.g. on every remote-client heartbeat.

    `reason` is "" whenever a SHA was found, and otherwise one of the
    COMMIT_* constants above explaining why there isn't one. The distinction
    matters to the admin console: COMMIT_NO_CHECKOUT is the normal state of a
    freshly installed machine (the .bat/.command scripts copy files rather
    than cloning, so .git only appears after apply_update() bootstraps it),
    not a fault, whereas COMMIT_GIT_UNREADABLE means a checkout is there but
    unreadable and worth looking at.
    """
    if not (_REPO_ROOT / ".git").exists():
        return "", COMMIT_NO_CHECKOUT
    try:
        args = ["git", "rev-parse", "--short", "HEAD"] if short else ["git", "rev-parse", "HEAD"]
        proc = subprocess.run(
            args, cwd=str(_REPO_ROOT), capture_output=True, text=True, timeout=5,
        )
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip(), ""
        log.debug("[Update] git rev-parse failed (rc=%s): %s",
                  proc.returncode, proc.stderr.strip()[:200])
    except Exception as e:
        log.debug("[Update] git rev-parse could not run: %s", e)

    sha = _read_sha_from_git_dir()
    if sha:
        return (sha[:7] if short else sha), ""
    return "", COMMIT_GIT_UNREADABLE


def get_local_commit_sha(short: bool = True) -> str:
    """This checkout's current HEAD commit, or "" if it can't be determined.
    Thin wrapper over get_commit_report() for callers that don't need to know
    why it's missing."""
    return get_commit_report(short)[0]


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


# ── Plain-English summary of a pending update ────────────────────────────────
# The header's "Update Available" popup used to show nothing but the raw commit
# subjects (and "New commits are available." when even those were missing),
# which says very little to whoever is actually running the app. These two
# functions turn HEAD..origin/<branch> into a short "here's what changes for
# you" blurb via the configured AI provider (Settings > AI), with the commit
# subjects kept as the fallback whenever no provider is configured or the call
# fails -- the update itself must never depend on an LLM being reachable.

_DIGEST_MAX_CHARS = 12000  # roughly 3k tokens; plenty for a normal update

_SUMMARY_SYSTEM = (
    "You explain a pending software update to the trader who runs this app on "
    "their own machine. You are given the git commit log for the update. "
    "Reply with 3-6 single-line bullet points, each starting with '- ', and "
    "nothing else: no preamble, no closing line, no headings, no commit "
    "hashes, no file names. Group related commits into one bullet. Say what "
    "each change means in practice for someone using the app. Fold purely "
    "internal work (tests, refactoring, logging, docs) into a single final "
    "bullet rather than one bullet each."
)


async def changes_digest(local_sha: str, remote_sha: str, max_commits: int = 40) -> str:
    """Commit messages plus a file-level diffstat for local_sha..remote_sha,
    trimmed to _DIGEST_MAX_CHARS -- the raw material summarise_changes() feeds
    to the LLM. "" if the range can't be read (not a checkout, unknown SHA)."""
    if not local_sha or not remote_sha:
        return ""

    rc, log_out, _ = await _run_git(
        "log", f"{local_sha}..{remote_sha}", "--no-merges",
        f"-n{max_commits}", "--pretty=format:* %s%n%b",
    )
    if rc != 0 or not log_out.strip():
        return ""

    parts = ["Commits in this update (newest first):", log_out.strip()]
    rc, stat_out, _ = await _run_git("diff", "--stat", f"{local_sha}..{remote_sha}")
    if rc == 0 and stat_out.strip():
        parts += ["", "Files changed:", stat_out.strip()]

    digest = "\n".join(parts)
    if len(digest) > _DIGEST_MAX_CHARS:
        digest = digest[:_DIGEST_MAX_CHARS] + "\n[... truncated ...]"
    return digest


async def summarise_changes(
    local_sha: str, remote_sha: str, cfg: Optional[dict] = None, timeout: int = 45,
) -> tuple[list[str], str]:
    """(bullets, error) describing what the pending update changes.

    `bullets` is a list of plain-English one-liners with no leading marker.
    On any failure it comes back empty and `error` says why, so the caller can
    fall back to the raw commit subjects -- callers must treat a summary as a
    nicety, never as a precondition for updating.
    """
    from backend.src.services.ai import provider

    if cfg is None:
        import backend.src.config as cfg_module
        cfg = cfg_module.load()

    if not ai_provider.is_configured(cfg):
        return [], "no AI provider configured (Settings > AI)"

    digest = await changes_digest(local_sha, remote_sha)
    if not digest:
        return [], "no commit details available"

    try:
        text = await ai_provider.complete(
            cfg, _SUMMARY_SYSTEM, digest, max_tokens=700, timeout=timeout,
        )
    except Exception as e:
        log.debug("[Update] change summary failed: %s", e)
        return [], f"{type(e).__name__}: {e}"[:200]

    bullets = []
    for line in text.splitlines():
        line = line.strip().lstrip("-*•").strip()
        if line:
            bullets.append(line)
    if not bullets:
        return [], "empty summary from AI provider"
    return bullets, ""


async def commit_summary(sha: str) -> str:
    """One-line commit message for `sha` in this checkout, "" on any failure
    (unknown sha, not a git checkout, etc). Used by the admin console's
    Versions tab to show what a commit SHA actually is, not just its hash."""
    if not sha:
        return ""
    rc, out, _ = await _run_git("log", "-1", "--pretty=%s", sha)
    return out.strip() if rc == 0 else ""


async def commits_behind(sha: str) -> Optional[int]:
    """How many commits `sha` is behind origin/<branch> in this checkout.
    None (not 0) if `sha` can't be resolved at all -- a client on a fork/
    stale ref this checkout has never fetched, not a client that's merely
    caught up. Caller should already have fetched (check_for_update() does
    this) so origin/<branch> is current; this does not fetch on its own."""
    if not sha:
        return None
    rc, out, _ = await _run_git("rev-list", "--count", f"{sha}..origin/{_BRANCH}")
    if rc != 0:
        return None
    try:
        return int(out.strip())
    except ValueError:
        return None


async def apply_update(restart: bool = True) -> dict:
    """Bootstrap a git checkout if this install doesn't have one yet, then
    fetch + force-checkout to origin/<branch>, reinstall requirements.txt
    into the venv, and clear stale __pycache__ dirs.

    This is the one code path both update entry points converge on: the
    client's own Settings > Update button, and the admin console's Update
    button (which now just sends MSG_GIT_UPDATE over the WS connection and
    asks the client to run this). Bootstrapping here means it works
    identically whichever one triggers it and however the machine was
    originally set up -- the Setup & Start .bat/.command scripts never
    git-clone anything, they just copy/run files, so a freshly installed
    machine has no .git at all until its first update.

    Uses `checkout -B <branch> --track origin/<branch> -f` rather than
    `pull --ff-only`: no client machine is meant to carry independent local
    commits, each one just mirrors origin, so forcing the working tree to
    match origin exactly is the correct, expected outcome regardless of any
    local drift -- not data loss. (Verified this handles both a from-empty
    bootstrap and an already-dirty existing checkout identically, discarding
    the dirty file and landing cleanly on origin's HEAD either way.)
    Anything a client actually needs to keep -- config.yaml, the licence
    store, .venv, the DB -- is already gitignored and untouched by this.

    Restarts on success (both a clean run and one where the post-update pip
    install / pycache sweep failed -- see below) unless `restart=False`.
    2026-09-03: leaving that to the caller meant a live install ran for ~40
    minutes against a venv this function had already reinstalled dependencies
    into, because nothing forced the restart to actually happen -- every
    outbound HTTPS call (Telegram alerts, AI commentary, the daily email)
    failed silently with a stale-module error for that whole window while 5
    real trades opened with no notification at all. `restart=False` exists
    only for the admin-console-triggered path (cluster/remote/_update.py),
    which runs its own restart sequence (Windows icon refresh, then a hard
    process exit with the bat-loop's relaunch code) and must keep doing that
    instead of being pre-empted here.

    Returns {"ok": bool, "error": str|None}.
    """
    if not (_REPO_ROOT / ".git").exists():
        if not shutil.which("git"):
            return {"ok": False, "error": "git not found on PATH — cannot bootstrap a checkout"}
        rc, out, err = await _run_git("init")
        if rc != 0:
            return {"ok": False, "error": (err.strip() or out.strip() or "git init failed")[:300]}
        rc, out, err = await _run_git("remote", "add", "origin", f"{_GITHUB_REPO_URL}.git")
        if rc != 0:
            return {"ok": False, "error": (err.strip() or out.strip() or "git remote add failed")[:300]}

    rc, out, err = await _run_git("fetch", "origin", _BRANCH)
    if rc != 0:
        return {"ok": False, "error": (err.strip() or out.strip() or "git fetch failed")[:300]}

    rc, out, err = await _run_git("checkout", "-B", _BRANCH, "--track", f"origin/{_BRANCH}", "-f")
    if rc != 0:
        return {"ok": False, "error": (err.strip() or out.strip() or "git checkout failed")[:300]}

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

    def _deploy_ea() -> None:
        """Push the EA the pull just brought into every MetaTrader terminal on
        this machine, so a remote user gets a new EA without touching
        anything. Best-effort by construction: see ea_deploy.deploy_after_
        update, which swallows its own failures -- the pull has already
        succeeded here, and a terminal that could not be written to is the
        state the machine was in a moment ago anyway. It cannot compile on
        macOS (MetaEditor does nothing headless under CrossOver), so a repo
        carrying a pre-compiled .ex5 is what makes this complete there."""
        from backend.src.services.broker import ea_deploy
        ea_deploy.deploy_after_update()

    try:
        await asyncio.to_thread(_pip_install)
        await asyncio.to_thread(_clear_pycache)
        await asyncio.to_thread(_deploy_ea)
    except Exception as e:
        log.warning("[Update] pip install / cache clear step failed: %s", e)
        # The pull already succeeded -- surface this as a soft warning, not
        # a hard failure, since a stale dependency/cache issue is
        # recoverable (Save & Restart again) whereas re-pulling isn't safe
        # to retry blindly. Restarting below is that "Save & Restart" --
        # automatic now instead of waiting on the caller to notice and click it.
        if restart:
            _restart()
        return {"ok": True, "error": f"update pulled, but post-update step failed: {e}"}

    if restart:
        _restart()
    return {"ok": True, "error": None}
