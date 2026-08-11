"""Move tests/core/ into a tree that mirrors backend/src/.

    python -m tools.test_layout.migrate            # dry run: print the plan
    python -m tools.test_layout.migrate --review   # only the files needing a human
    python -m tools.test_layout.migrate --apply    # git mv, for real

**Why.** `backend/src/core/` does not exist. It was dissolved into
`backend/src/services/<domain>/` during the 2026 refactor (see
docs/todo/refactor/stage0/, 49 `core-*-migration` folders) and the tests did
not follow. `tests/core/` is now 127 files -- 75% of the suite -- in one flat
directory named after a deleted module, while `tests/services/` holds 5.

**What this does and does not touch.** It is `git mv` and nothing else. No
assertion is edited, no import is rewritten, no file is renamed. That is safe
here for three reasons, each checked rather than assumed:

  * No test module imports another test module (grepped: zero hits), so moving
    a file cannot break a sibling's import.
  * `mock.patch` targets are dotted strings naming *source* modules. Where the
    test file sits is irrelevant to them.
  * The coverage ratchet buckets by source path (`coverage_gate.area_of`), so
    every per-area floor is unchanged by definition. This migration cannot move
    a coverage number.

The one thing that does change: `__init__.py` must exist in every destination
directory. Basenames collide across service dirs -- `test_repo_transactions.py`
already exists three times -- and without `__init__.py` pytest imports them as
one top-level module and silently runs only the last one. `tests/reversal_engine/`
is missing its `__init__.py` today; this creates it.

**How a destination is chosen.** Two rules, in order, then a human:

  1. The test's filename (minus `test_` and any `_characterization` /
     `_surface` / `_relocation` suffix) matches exactly one module under
     `backend/src/`. Strongest signal -- 51 files.
  2. Otherwise, the most-imported `backend.src.services.<domain>` in the file.
     62 files. `backend.src.db` is excluded from this count: nearly every test
     imports it for the `fresh_db` fixture, so it dominates by frequency while
     saying nothing about the subject.
  3. Neither resolves -> listed under `--review`. 14 files. Do not apply
     without reading those.

Run the suite before and after and diff the counts. They must match exactly.
"""
from __future__ import annotations

import argparse
import collections
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "backend" / "src"
LEGACY = REPO_ROOT / "tests" / "core"
TESTS = REPO_ROOT / "tests"

# Areas that are not under services/ but still mirror a real source location.
TOP_LEVEL = ("db", "utils", "config", "controllers", "runtime", "app")

_IMPORT = re.compile(
    r"backend\.src\.((?:services\.[a-z_]+)|db|utils|config|controllers|runtime|app)"
)
_TYPE_SUFFIXES = ("_characterization", "_surface", "_relocation")


def _source_modules() -> dict[str, set[str]]:
    """stem -> set of directories (relative to backend/src) defining it."""
    out: dict[str, set[str]] = {}
    for p in SRC.rglob("*.py"):
        if "__pycache__" in p.parts or p.name == "__init__.py":
            continue
        out.setdefault(p.stem, set()).add(p.relative_to(SRC).parent.as_posix())
    return out


def _subject(name: str) -> str:
    """test_open_trade_surface.py -> open_trade"""
    stem = name[len("test_"):-len(".py")]
    for suffix in _TYPE_SUFFIXES:
        if stem.endswith(suffix):
            return stem[: -len(suffix)]
    return stem


def plan() -> tuple[dict[str, str], dict[str, str], list[str]]:
    """Returns (destination by filename, rule by filename, needs-review list)."""
    mods = _source_modules()
    dest: dict[str, str] = {}
    rule: dict[str, str] = {}
    review: list[str] = []

    for f in sorted(LEGACY.glob("test_*.py")):
        dirs = mods.get(_subject(f.name))
        if dirs and len(dirs) == 1:
            dest[f.name] = next(iter(dirs))
            rule[f.name] = "filename matches one src module"
            continue

        counts = collections.Counter(
            _IMPORT.findall(f.read_text(encoding="utf-8", errors="replace"))
        )
        services = {k: v for k, v in counts.items() if k.startswith("services.")}
        if services:
            dest[f.name] = max(services, key=services.get).replace(".", "/")
            rule[f.name] = "dominant services import"
            continue

        # `db` excluded: the fresh_db fixture makes it ubiquitous and meaningless.
        other = {k: v for k, v in counts.items() if k != "db"}
        dest[f.name] = max(other, key=other.get).replace(".", "/") if other else "db"
        rule[f.name] = "UNRESOLVED - read this file"
        review.append(f.name)

    # Rule 3, resolution pass: a characterization test written against the old
    # SimulationEngine god object imports `backend.src.runtime` and names no
    # service at all, so rules 1 and 2 both land it in tests/runtime/ -- wrong.
    # Its subject is whatever was extracted out of the god object, and its
    # `_surface` twin already resolved to exactly that. Follow the twin.
    for name in list(review):
        subject = _subject(name)
        twins = [
            other
            for other in dest
            if other != name
            and _subject(other) == subject
            and not rule[other].startswith("UNRESOLVED")
        ]
        if len(set(dest[t] for t in twins)) == 1:
            dest[name] = dest[twins[0]]
            rule[name] = f"follows twin {twins[0]}"
            review.remove(name)

    return dest, rule, review


def _print_plan(dest: dict[str, str], rule: dict[str, str]) -> None:
    by_dir: dict[str, list[str]] = collections.defaultdict(list)
    for name, d in dest.items():
        by_dir[d].append(name)
    for d in sorted(by_dir, key=lambda k: (-len(by_dir[k]), k)):
        print(f"\ntests/{d}/  ({len(by_dir[d])})")
        for name in sorted(by_dir[d]):
            flag = "  <-- REVIEW" if rule[name].startswith("UNRESOLVED") else ""
            print(f"    {name}{flag}")
    print(f"\n{len(dest)} files -> {len(by_dir)} directories")
    counts = collections.Counter(rule.values())
    for r, n in counts.most_common():
        print(f"  {n:4}  {r}")


def apply(dest: dict[str, str]) -> int:
    """git mv every file, creating package dirs as needed."""
    moved = 0
    for name, d in sorted(dest.items()):
        target_dir = TESTS / Path(d)
        target_dir.mkdir(parents=True, exist_ok=True)
        init = target_dir / "__init__.py"
        if not init.exists():
            init.write_text("", encoding="utf-8")
            subprocess.run(["git", "add", str(init)], cwd=REPO_ROOT, check=True)
        subprocess.run(
            ["git", "mv", str(LEGACY / name), str(target_dir / name)],
            cwd=REPO_ROOT,
            check=True,
        )
        moved += 1

    # Every existing test directory needs one too, for the same collision reason.
    for d in TESTS.rglob("*"):
        if d.is_dir() and d.name != "__pycache__" and not (d / "__init__.py").exists():
            (d / "__init__.py").write_text("", encoding="utf-8")
            subprocess.run(["git", "add", str(d / "__init__.py")], cwd=REPO_ROOT, check=True)
            print(f"created missing {d.relative_to(REPO_ROOT)}/__init__.py")

    return moved


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="perform the git mv")
    ap.add_argument("--review", action="store_true", help="list only unresolved files")
    args = ap.parse_args()

    if not LEGACY.exists():
        print("tests/core/ does not exist - migration already done.")
        return 0

    dest, rule, review = plan()

    if args.review:
        print(f"{len(review)} files need a human decision:\n")
        for name in review:
            print(f"  {name}\n      best guess: tests/{dest[name]}/")
        return 0

    if not args.apply:
        _print_plan(dest, rule)
        print(
            f"\nDRY RUN - nothing moved. {len(review)} files need review "
            f"(--review). Re-run with --apply once you have read them."
        )
        return 0

    if review:
        print(
            f"Refusing to apply: {len(review)} files are unresolved.\n"
            "Run --review, decide their homes, then encode them here."
        )
        return 1

    moved = apply(dest)
    print(f"\nMoved {moved} files. Now:")
    print("  pytest tests/ -q            # counts must match the pre-move run")
    print("  python -m tools.checks all")
    return 0


if __name__ == "__main__":
    sys.exit(main())
