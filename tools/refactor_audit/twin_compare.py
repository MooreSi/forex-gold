#!/usr/bin/env python3
"""Classifies each orphan against the inline copy still running in engine.py.

An orphaned extraction is only a *correctness* bug if a live inline twin
exists, and the disposition depends on whether the two agree:

  identical  -> the inline copy is live and tested; delete the orphan, and
                re-extract properly in the phase that owns that domain.
  diverged   -> the difference is a bug report. Decide which behaviour is
                correct, fix the LIVE inline copy, then delete the orphan.
  no twin    -> the logic exists only in the dead module. Nothing is running
                it, so it is either genuinely unused or a wiring gap.

Usage:
    python tools/refactor_audit/twin_compare.py
    python tools/refactor_audit/twin_compare.py --diff      # show the diffs
    python tools/refactor_audit/twin_compare.py --json
"""
from __future__ import annotations

import argparse
import ast
import difflib
import json
import sys
from pathlib import Path

from tools.refactor_audit import orphan_detector as od
from tools.refactor_audit.ast_normalise import (
    decorator_names,
    find_function,
    normalised_source,
    statement_shape,
)

ENGINE_PATH = od.REPO_ROOT / "backend" / "src" / "runtime.py"


def candidate_names(function: str) -> list[str]:
    """Extraction usually drops or keeps a leading underscore, nothing more."""
    bare = function.lstrip("_")
    return [function, f"_{bare}", bare]


def compare(orphan: dict, engine_tree: ast.AST) -> dict:
    module_path = od.REPO_ROOT / orphan["file"]
    extracted = find_function(
        ast.parse(module_path.read_text(encoding="utf-8")), orphan["function"]
    )
    assert extracted is not None, f"{orphan['module']}::{orphan['function']} vanished"

    twin = twin_name = None
    for name in candidate_names(orphan["function"]):
        if (found := find_function(engine_tree, name)) is not None:
            twin, twin_name = found, name
            break

    result = {**orphan, "twin": twin_name, "verdict": "no twin", "notes": []}
    if twin is None:
        return result

    result["twin_line"] = twin.lineno
    result["twin_loc"] = (twin.end_lineno or twin.lineno) - twin.lineno + 1

    extracted_src = normalised_source(extracted)
    twin_src = normalised_source(twin)

    if extracted_src == twin_src:
        result["verdict"] = "identical"
    else:
        result["verdict"] = "diverged"
        result["diff"] = "\n".join(difflib.unified_diff(
            twin_src.splitlines(), extracted_src.splitlines(),
            fromfile=f"engine.py::{twin_name} (LIVE)",
            tofile=f"{orphan['module']}::{orphan['function']} (dead)",
            lineterm="",
        ))

    if (a := statement_shape(twin)) != (b := statement_shape(extracted)):
        result["notes"].append(
            f"statement count differs: live={len(a)} dead={len(b)} "
            f"({'dead is missing statements' if len(b) < len(a) else 'dead has extra statements'})"
        )
    if (a := decorator_names(twin)) != (b := decorator_names(extracted)):
        result["notes"].append(f"decorators differ: live={a} dead={b}")

    return result


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--diff", action="store_true", help="print the normalised diffs")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    engine_tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    results = [compare(o, engine_tree) for o in od.find_orphans()]

    if args.json:
        print(json.dumps(results, indent=2))
        return 0

    print(f"{'VERDICT':<10}  {'LOC':>4}  {'MODULE::FUNCTION':<52}  LIVE TWIN")
    for r in results:
        ident = f"{r['module']}::{r['function']}"
        twin = f"engine.py:{r['twin_line']} {r['twin']}" if r.get("twin") else "-"
        print(f"{r['verdict']:<10}  {r['loc']:>4}  {ident:<52}  {twin}")
        for note in r["notes"]:
            print(f"{'':>12}  ! {note}")

    if args.diff:
        for r in results:
            if r.get("diff"):
                print(f"\n{'=' * 78}\n{r['module']}::{r['function']}\n{'=' * 78}")
                print(r["diff"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
