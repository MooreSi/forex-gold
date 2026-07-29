#!/usr/bin/env python3
"""Parses every Python file in the repo. Nothing more.

This exists because a bulk `sed` import rewrite broke the tree twice during the
same refactor, both times the same way: a rule written for
`from X import Y` was applied to `from X import Y as Z` and produced

    from backend.src.services.analytics import reporting as reporting as rep

Both times the damage was invisible until pytest tried to collect, and the
second time was after the lesson had already been written down. A check is
cheaper than remembering.

Run it immediately after any mechanical rewrite, before the test suite -- it
takes about a second and localises the break precisely, where a collection
error reports only the first file pytest happened to reach.

    python3 -m tools.refactor_audit.check_syntax
"""
from __future__ import annotations

import ast
import sys
from pathlib import Path

from tools.refactor_audit import orphan_detector as od

SKIP_DIRS = {".git", "__pycache__", ".venv", "venv", "node_modules"}


def broken_files() -> list[tuple[Path, int, str]]:
    out = []
    for path in sorted(od.REPO_ROOT.rglob("*.py")):
        if any(part in SKIP_DIRS for part in path.parts):
            continue
        try:
            ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError as exc:
            out.append((path.relative_to(od.REPO_ROOT), exc.lineno or 0, exc.msg))
        except UnicodeDecodeError as exc:
            out.append((path.relative_to(od.REPO_ROOT), 0, f"undecodable: {exc}"))
    return out


def main() -> int:
    broken = broken_files()
    if not broken:
        print("All Python files parse.")
        return 0
    print(f"FAIL: {len(broken)} file(s) do not parse:", file=sys.stderr)
    for path, line, msg in broken:
        print(f"  {path}:{line}  {msg}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
