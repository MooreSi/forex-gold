#!/usr/bin/env python3
"""Finds extractions that were WIRED IN but silently diverged from the original.

This is the opposite failure to the orphan detector. There, the module is dead.
Here it is live -- and quietly missing statements the original had. Two cases
are already documented and neither was caught by any test:

  core_run_tp_ladder.py    lost a `current_sl = new_sl` state update and a
                           "SL moved to breakeven" Telegram alert
                           (core-engine-wiring/PROGRESS.md:509-530)
  core_handle_orb_fixed.py lost a trailing log.info  (PROGRESS.md:625-640)

The repo history makes this mechanical: the refactor landed one commit per
pack, so for any extracted module we can recover both sides of the extraction.

  original = the method in engine.py at the extraction commit's PARENT
  copy     = the extracted function at the extraction commit

Comparing those two catches the gap at the moment it was introduced. Comparing
the copy at the extraction commit against the copy at HEAD then separates an
extraction gap from a legitimate later change -- 32 feature commits landed
after the refactor finished, so "differs from the original" is not by itself
evidence of a bug.

Usage:
    python tools/refactor_audit/divergence_detector.py
    python tools/refactor_audit/divergence_detector.py --diff
    python tools/refactor_audit/divergence_detector.py --module core_run_tp_ladder
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import subprocess
import sys
from pathlib import Path

from tools.refactor_audit import orphan_detector as od
from tools.refactor_audit.ast_normalise import (
    decorator_names,
    find_function,
    normalised_source,
    statement_shape,
)

# The files the extractions were carved out of.
SOURCE_FILES = ("forex_trader/core/engine.py", "forex_trader/core/database.py")


def git(*args: str) -> str | None:
    result = subprocess.run(
        ["git", *args], cwd=od.REPO_ROOT,
        capture_output=True, text=True,
    )
    return result.stdout if result.returncode == 0 else None


def extraction_commit(rel_path: str) -> str | None:
    """The commit that first added this file."""
    out = git("log", "--diff-filter=A", "--format=%H", "--", rel_path)
    if not out or not out.strip():
        return None
    return out.strip().splitlines()[-1]


def parse_at(commit: str, rel_path: str) -> ast.AST | None:
    src = git("show", f"{commit}:{rel_path}")
    if src is None:
        return None
    try:
        return ast.parse(src)
    except SyntaxError:
        return None


def candidate_names(name: str) -> list[str]:
    bare = name.lstrip("_")
    return [name, f"_{bare}", bare]


def find_original(trees: dict[str, ast.AST], name: str):
    for path, tree in trees.items():
        for candidate in candidate_names(name):
            if (found := find_function(tree, candidate)) is not None:
                return found, path, candidate
    return None, None, None


# Losing @staticmethod is the expected consequence of turning a method into a
# module-level function. It is not the @contextmanager class of defect.
EXPECTED_DECORATOR_LOSS = {"staticmethod", "classmethod"}


def missing_statements(original_src: str, copy_src: str,
                       module_lines: set[str]) -> list[str]:
    """Lines in the original that appear nowhere in the extracted module.

    Comparing only against the matching function produces false mass-deletions:
    several extractions split one method into a public function plus private
    helpers in the same module, so the helpers' statements read as "lost" when
    they simply moved next door. Checking against every line in the module
    keeps genuine truncations visible while dropping that noise.
    """
    removed = [
        line[2:] for line in difflib.unified_diff(
            original_src.splitlines(), copy_src.splitlines(), n=0, lineterm="")
        if line.startswith("-") and not line.startswith("---")
    ]
    return [line for line in removed if line.strip() not in module_lines]


def module_statement_lines(tree: ast.AST) -> set[str]:
    """Every normalised source line in the module, from any function."""
    lines: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            lines.update(l.strip() for l in normalised_source(node).splitlines())
    return lines


_LIVE_CORPUS: set[str] | None = None


def live_corpus() -> set[str]:
    """Every normalised statement line in production code at HEAD.

    One method was frequently carved into several modules at once -- the
    pre-trade validation that used to sit inside `open_trade_from_signal` now
    lives in core_signal_resolution.py, not in core_open_trade_from_signal.py.
    Without this, that reads as a 133-statement truncation.

    A line surviving somewhere at HEAD is not proof it is still *reached* --
    that is the orphan detector's job. The two checks are complementary: this
    one asks "did the logic survive the move", that one asks "does anything
    call it".
    """
    global _LIVE_CORPUS
    if _LIVE_CORPUS is None:
        corpus: set[str] = set()
        for path in od.production_files():
            try:
                corpus |= module_statement_lines(
                    ast.parse(path.read_text(encoding="utf-8")))
            except (SyntaxError, UnicodeDecodeError):
                continue
        _LIVE_CORPUS = corpus
    return _LIVE_CORPUS


def audit_module(path: Path, historical: bool = False) -> list[dict]:
    # git wants POSIX separators; str() yields backslashes on Windows and every
    # rev-list/show lookup against them silently finds nothing.
    rel = path.relative_to(od.REPO_ROOT).as_posix()
    commit = extraction_commit(rel)
    if commit is None:
        return [{"module": path.stem, "status": "no extraction commit"}]

    copy_tree = parse_at(commit, rel)
    if copy_tree is None:
        return [{"module": path.stem, "status": "unparseable at extraction commit"}]

    originals = {}
    for source in SOURCE_FILES:
        if (tree := parse_at(f"{commit}^", source)) is not None:
            originals[source] = tree
    if not originals:
        return [{"module": path.stem, "status": "no source file at parent commit"}]

    head_tree = ast.parse(path.read_text(encoding="utf-8"))
    # Historical mode drops the survival corpus, so a gap that was introduced
    # at extraction and fixed later still shows. That is how this tool is
    # validated against the two documented truncations -- both of which are
    # fixed at HEAD and therefore invisible in the default mode.
    module_lines = module_statement_lines(copy_tree)
    if not historical:
        module_lines = module_lines | live_corpus()

    findings = []
    for node in copy_tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.name.startswith("_"):
            continue

        original, source_path, matched = find_original(originals, node.name)
        if original is None:
            # A helper invented during extraction, not carved out of anything.
            continue

        original_src = normalised_source(original)
        copy_src = normalised_source(node)
        if original_src == copy_src:
            continue

        finding = {
            "module": path.stem,
            "function": node.name,
            "commit": commit[:9],
            "source": f"{Path(source_path).name}::{matched}",
            "missing": missing_statements(original_src, copy_src, module_lines),
            "shape_delta": len(statement_shape(node)) - len(statement_shape(original)),
            "decorators_lost": sorted(
                (set(decorator_names(original)) - set(decorator_names(node)))
                - EXPECTED_DECORATOR_LOSS),
            "changed_since_extraction": False,
        }

        # Did it change after extraction? If so, the gap may be a later edit.
        if (head_fn := find_function(head_tree, node.name)) is not None:
            finding["changed_since_extraction"] = (
                normalised_source(head_fn) != copy_src)

        finding["diff"] = "\n".join(difflib.unified_diff(
            original_src.splitlines(), copy_src.splitlines(),
            fromfile=f"{finding['source']} @ {commit[:9]}^ (ORIGINAL)",
            tofile=f"{path.stem}::{node.name} @ {commit[:9]} (EXTRACTED)",
            lineterm="",
        ))
        findings.append(finding)
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--module", help="audit a single module, e.g. core_run_tp_ladder")
    parser.add_argument("--diff", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--historical", action="store_true",
                        help="report gaps as they were at extraction time, ignoring "
                             "whether the logic survives anywhere at HEAD")
    args = parser.parse_args()

    pattern = f"{args.module}.py" if args.module else "core_*.py"
    modules = sorted(od.CORE_DIR.glob(pattern))
    if not modules:
        print(f"no modules match {pattern}", file=sys.stderr)
        return 2

    all_findings = []
    for path in modules:
        all_findings.extend(audit_module(path, historical=args.historical))

    # Rank by truncation risk. A copy that lost statements outright is the
    # core_run_tp_ladder signature; one that merely differs in wording is
    # usually the mechanical parameter/context rewrite.
    real = sorted(
        (f for f in all_findings if f.get("missing") or f.get("decorators_lost")),
        key=lambda f: (f["shape_delta"], -len(f["missing"])),
    )

    if args.json:
        print(json.dumps(all_findings, indent=2))
        return 0

    print(f"Audited {len(modules)} module(s); "
          f"{len(real)} function(s) differ from the original they were carved from.\n")
    if real:
        print(f"{'LOST':>4}  {'Δ':>3}  {'MODULE::FUNCTION':<52}  ORIGIN")
        for f in real:
            ident = f"{f['module']}::{f['function']}"
            flag = " *later-edit" if f["changed_since_extraction"] else ""
            print(f"{len(f['missing']):>4}  {f['shape_delta']:>+3}  {ident:<52}  "
                  f"{f['source']}{flag}")
            for dec in f["decorators_lost"]:
                print(f"{'':>11}  ! decorator lost: @{dec}")
        print("\n  LOST = normalised lines present in the original, absent in the copy")
        print("  Δ    = statement-count delta (negative means the copy is smaller)")
        print("  *later-edit = the copy changed after extraction, so the gap may be"
              " a deliberate later change rather than a truncation")

    if args.diff:
        for f in real:
            print(f"\n{'=' * 78}\n{f['module']}::{f['function']}\n{'=' * 78}")
            print(f["diff"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
