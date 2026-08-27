"""The repo-wide scanners must not walk into git worktrees.

Claude Code creates agent worktrees under .claude/worktrees/<name>/, and a
worktree is a COMPLETE second checkout of this repo. Every scanner that walks
REPO_ROOT therefore sees two copies of every production file.

That is not a cosmetic duplicate. The scanners key their findings on relative
paths, so the second copy is not recognised as the same file:

  - the LOC gate reports ".claude/worktrees/X/backend/src/runtime.py" as a NEW
    violation, because that path is not in structure_baseline.json
  - the orphan-module gate reports ~300 unrecorded orphans, because nothing in
    the real tree imports ".claude.worktrees.X.backend.src.services..."
  - the transaction gate reports offenders in the worktree's database.py
  - the duplicate-implementation detector finds every function twice

Observed 2026-08-27: spawning one background task took four gates from green
to red with several hundred phantom entries, none of them real. A gate that
goes red for reasons the reader cannot act on is worse than no gate, because
the next real failure gets scrolled past with the noise.

CLAUDE.md already lists the exclusions repo-wide scripts need (.git, .venv,
__pycache__, docs/todo/refactor/stage0/, docs/reviews/). Worktrees are the
newest member of that list.
"""
from __future__ import annotations

from tools.refactor_audit import orphan_detector as od
from tools.refactor_audit import orphan_modules as om


def test_both_scanners_exclude_the_claude_directory():
    """Pinned by name so the exclusion survives a rewrite of either set.

    .claude as a whole rather than .claude/worktrees specifically: nothing
    under it is production code, and the parts-based matching these scanners
    use works on a single path component.
    """
    assert ".claude" in od.EXCLUDED_DIRS
    assert ".claude" in om.EXCLUDED_DIRS


def test_no_scanned_production_file_lives_under_a_worktree():
    """The behaviour, not just the constant.

    This is the assertion that was actually red: with a worktree present and
    .claude unexcluded, production_files() returns both copies of everything.
    """
    offenders = [
        p for p in od.production_files()
        if ".claude" in p.relative_to(od.REPO_ROOT).parts
    ]
    assert offenders == [], (
        f"{len(offenders)} scanned files live under .claude/ -- the scanner is "
        f"walking a git worktree and will double-count the whole repo. "
        f"First few: {[p.as_posix() for p in offenders[:3]]}"
    )


def test_the_real_tree_is_still_scanned():
    """Negative control. An over-broad exclusion that returned nothing would
    make every gate pass for the wrong reason -- which is the exact failure
    mode CLAUDE.md's 'green output is not evidence' warning is about.
    """
    scanned = {p.relative_to(od.REPO_ROOT).as_posix() for p in od.production_files()}
    assert "backend/src/runtime.py" in scanned
    assert "mt5_bridge.py" in scanned
    assert len(scanned) > 200, f"only {len(scanned)} files scanned"
