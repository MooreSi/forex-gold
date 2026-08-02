#!/usr/bin/env python3
"""The M4 dissolution ratchet -- delegation_checker's successor.

delegation_checker guarded the extraction era: every engine method with a
core_* twin had to delegate to it. That premise dissolved with
forex_trader/core; its glob finds nothing and it passes vacuously. What the
dissolution era needs guarded is the opposite direction:

  1. the runtime class only ever SHRINKS (method count is a shrink-only
     baseline, same philosophy as structure_gates), and
  2. its public surface is exactly the curated facade allowlist -- names
     may be removed from the allowlist, never added.

Usage:
    python -m tools.refactor_audit.facade_audit             # report
    python -m tools.refactor_audit.facade_audit --check     # CI
    python -m tools.refactor_audit.facade_audit --update-baseline
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

from tools.refactor_audit import orphan_detector as od
from tools.refactor_audit.delegation_checker import is_wrapper

RUNTIME_PATH   = od.REPO_ROOT / "backend" / "src" / "runtime.py"
BASELINE_PATH  = Path(__file__).parent / "facade_baseline.json"
ALLOWLIST_PATH = Path(__file__).parent / "facade_allowlist.json"

# One constant so the SimulationEngine -> TradingRuntime rename is one edit.
CLASS_NAME = "SimulationEngine"


def census(source: str) -> dict[str, dict]:
    """{method_name: {"public": bool, "wrapper": bool}} for CLASS_NAME."""
    tree = ast.parse(source)
    cls = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == CLASS_NAME),
        None,
    )
    if cls is None:
        return {}
    out: dict[str, dict] = {}
    for node in cls.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            out[node.name] = {
                "public": not node.name.startswith("_"),
                "wrapper": is_wrapper(node),
            }
    return out


def check(c: dict[str, dict], baseline: dict, allowlist: set[str]) -> list[str]:
    failures: list[str] = []
    ceiling = baseline.get("method_count")
    if ceiling is not None and len(c) > ceiling:
        failures.append(
            f"[facade] method count grew: {len(c)} > baseline {ceiling} "
            f"-- the runtime class only shrinks"
        )
    for name, info in sorted(c.items()):
        if info["public"] and name not in allowlist:
            failures.append(
                f"[facade] public method '{name}' is not in facade_allowlist.json "
                f"-- new public API on the runtime class is not allowed"
            )
    return failures


def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.exists():
        return set()
    return set(json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8")))


def update_baseline(c: dict[str, dict]) -> None:
    BASELINE_PATH.write_text(
        json.dumps({"method_count": len(c)}, indent=2) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="CI: exit 1 on regression")
    parser.add_argument("--update-baseline", action="store_true")
    args = parser.parse_args()

    c = census(RUNTIME_PATH.read_text(encoding="utf-8"))
    wrappers = sum(1 for i in c.values() if i["wrapper"])
    publics  = sum(1 for i in c.values() if i["public"])

    if args.update_baseline:
        update_baseline(c)
        print(f"Baseline written: {len(c)} methods")
        return 0

    failures = check(c, load_baseline(), load_allowlist())
    print(f"{CLASS_NAME}: {len(c)} methods ({publics} public, {wrappers} wrappers)")
    if failures:
        for f in failures:
            print(f"  {f}")
        if args.check:
            return 1
    else:
        print("OK: within baseline, public surface allowlisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
