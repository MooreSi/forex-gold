"""Verify every relative markdown link in the living docs resolves.

Run after ANY file move under docs/:

    python tools/check_doc_links.py

Exit 0 = all links resolve. Exit 1 = broken links, each printed as
`file: target`. Skipped on purpose: docs/todo/refactor/stage0/ (the
read-only audit trail — its links describe what was true at the time)
and external http(s)/mailto links.

This existed as a hand-rolled snippet re-typed four times during the
2026-08-11 docs reorganisation before becoming a tool. Anchors (#...)
are stripped, not validated.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LINK = re.compile(r"\]\(([^)#\s]+)(#[^)]*)?\)")
SKIP_PARTS = {"stage0"}


def broken_links() -> list[str]:
    targets = sorted((REPO / "docs").rglob("*.md"))
    targets += [REPO / "CLAUDE.md", REPO / "README.md"]
    out: list[str] = []
    for path in targets:
        if SKIP_PARTS & set(path.parts):
            continue
        for match in LINK.finditer(path.read_text(encoding="utf-8")):
            target = match.group(1)
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).exists():
                out.append(f"{path.relative_to(REPO)}: {target}")
    return out


def main() -> int:
    bad = broken_links()
    if bad:
        print(f"{len(bad)} broken link(s):")
        for line in bad:
            print(" ", line)
        return 1
    print("OK: all relative markdown links resolve (stage0 excluded).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
