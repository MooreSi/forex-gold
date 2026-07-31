#!/usr/bin/env python3
"""Finds SimulationEngine methods that should delegate to an extracted module
but still carry their own implementation.

This is Check 4, and it addresses the root cause of every finding in this
audit. The extraction packs produced two tests per module -- one driving the
engine method, one driving the extracted function -- and *nothing* asserting
the first calls the second. `_sync_closed_mt5_positions` is the proof: 273
lines of live inline code in engine.py, a 312-line extracted module nobody
imports, twelve passing tests, and a tracker row marked Done.

The check derives delegation from the code rather than from the tracker table.
The tracker's "Original method(s)" column is prose ("body (sleep/while shell
kept)", "fully replaced"), so parsing it would import the very unreliability
this pack exists to remove.

For each method on SimulationEngine we ask three questions:

  1. Does an extracted module define a function of the same name?
  2. If so, does the method actually call it?
  3. If not, is the body real logic, or just a delegating wrapper?

A method with a twin, that calls nothing, and that does its own work is an
unwired duplicate.

Usage:
    python -m tools.refactor_audit.delegation_checker
    python -m tools.refactor_audit.delegation_checker --check   # CI
    python -m tools.refactor_audit.delegation_checker --json
"""
from __future__ import annotations

import argparse
import ast
import json
import sys

from tools.refactor_audit import orphan_detector as od

ENGINE_PATH = od.REPO_ROOT / "backend" / "src" / "runtime.py"
ALLOWLIST_PATH = od.REPO_ROOT / "tools" / "refactor_audit" / "delegation_allowlist.json"

# A wrapper's whole body is "call the thing and hand back the result". Anything
# with its own branching or arithmetic is an implementation, however short.
#
# An earlier version of this ranked by statement count instead, and let
# `suggest_lot_size` through at exactly the cutoff -- ten statements of real
# risk-sizing logic classed as a harmless wrapper. Counting statements measures
# the wrong thing; what matters is whether the body does work.
WRAPPER_STATEMENT_LIMIT = 2


def statement_count(node: ast.AST) -> int:
    return sum(1 for n in ast.walk(node) if isinstance(n, ast.stmt))


def is_wrapper(method: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """True when the body is just a delegating call, with no logic of its own."""
    body = method.body
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        body = body[1:]
    if len(body) > WRAPPER_STATEMENT_LIMIT:
        return False
    return all(
        isinstance(stmt, (ast.Return, ast.Expr, ast.Pass))
        for stmt in body
    )


def extracted_functions() -> dict[str, set[str]]:
    """function name -> the core_* modules defining it."""
    index: dict[str, set[str]] = {}
    for path in sorted(od.CORE_DIR.glob("core_*.py")):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except SyntaxError:
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                index.setdefault(node.name, set()).add(path.stem)
    return index


def engine_import_map(tree: ast.AST) -> dict[str, tuple[str, str]]:
    """local name in engine.py -> (source module stem, original symbol).

    engine.py:39 does `from ...core_monitor_loop import check_sl as _check_sl_impl`,
    so the local name bears no resemblance to the target.
    """
    mapping: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            stem = node.module.rsplit(".", 1)[-1]
            if not stem.startswith("core_"):
                continue
            for alias in node.names:
                mapping[alias.asname or alias.name] = (stem, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                stem = alias.name.rsplit(".", 1)[-1]
                if stem.startswith("core_"):
                    mapping[alias.asname or stem] = (stem, "*")
    return mapping


def called_targets(method: ast.AST, imports: dict[str, tuple[str, str]]) -> set[tuple[str, str]]:
    """Every (module, symbol) this method reaches, via direct name or alias."""
    found: set[tuple[str, str]] = set()
    for node in ast.walk(method):
        if isinstance(node, ast.Name) and node.id in imports:
            found.add(imports[node.id])
        elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
            if (target := imports.get(node.value.id)) and target[1] == "*":
                found.add((target[0], node.attr))
    return found


def audit() -> list[dict]:
    tree = ast.parse(ENGINE_PATH.read_text(encoding="utf-8"))
    imports = engine_import_map(tree)
    index = extracted_functions()

    engine_cls = next(
        (n for n in ast.walk(tree)
         if isinstance(n, ast.ClassDef) and n.name == "SimulationEngine"), None)
    if engine_cls is None:
        raise SystemExit("SimulationEngine not found in engine.py")

    findings = []
    for method in engine_cls.body:
        if not isinstance(method, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue

        bare = method.name.lstrip("_")
        twins = index.get(bare, set()) | index.get(method.name, set())
        if not twins:
            continue

        reached = called_targets(method, imports)
        delegates_to = sorted(
            m for m in twins
            if (m, bare) in reached or (m, method.name) in reached
        )
        stmts = statement_count(method)

        if delegates_to:
            continue  # wired

        findings.append({
            "method": method.name,
            "line": method.lineno,
            "loc": (method.end_lineno or method.lineno) - method.lineno + 1,
            "statements": stmts,
            "twins": sorted(twins),
            "severity": "wrapper, no delegation" if is_wrapper(method)
                        else "unwired duplicate",
        })
    return sorted(findings, key=lambda f: -f["statements"])


def load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.exists():
        return set()
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return {e["method"] for e in data.get("allowed", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 on any unwired duplicate not in the allowlist")
    args = parser.parse_args()

    findings = audit()
    serious = [f for f in findings if f["severity"] == "unwired duplicate"]

    if args.json:
        print(json.dumps(findings, indent=2))
        return 0

    if not findings:
        print("Every SimulationEngine method with an extracted twin delegates to it.")
    else:
        print(f"{len(serious)} unwired duplicate(s), "
              f"{len(findings) - len(serious)} non-delegating wrapper(s):\n")
        print(f"{'STMTS':>5}  {'LOC':>5}  {'METHOD':<42}  EXTRACTED TWIN")
        for f in findings:
            mark = "!" if f["severity"] == "unwired duplicate" else " "
            print(f"{f['statements']:>5}  {f['loc']:>5}  {mark} {f['method']:<40}  "
                  f"{', '.join(f['twins'])}")
        print("\n  ! = a re-implementation: the extracted module exists, the method")
        print("      does not call it, and the logic runs from engine.py instead.")

    if args.check:
        allowed = load_allowlist()
        unexpected = [f for f in serious if f["method"] not in allowed]
        if unexpected:
            print(f"\nFAIL: {len(unexpected)} unwired duplicate(s) not allowlisted:",
                  file=sys.stderr)
            for f in unexpected:
                print(f"  {f['method']} (engine.py:{f['line']}, {f['statements']} "
                      f"statements) duplicates {', '.join(f['twins'])}", file=sys.stderr)
            return 1
        print(f"\nOK: {len(serious)} unwired duplicate(s), all allowlisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
