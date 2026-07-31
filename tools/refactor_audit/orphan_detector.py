#!/usr/bin/env python3
"""Finds extracted-but-never-wired functions in forex_trader/core/core_*.py.

The refactor documented in docs/todo/refactor/ extracted ~49 modules out of
core/engine.py and marked every wiring row "Done". At least four of those rows
are false: the module exists, its tests pass, and nothing in production ever
calls it -- while the original inline copy still runs in engine.py. Green tests
don't catch this, because each _surface.py test imports the extracted module
directly and each _characterization.py test drives the inline copy. Nothing
asserts the first calls the second.

Detection has to be AST-based and alias-aware. A text grep gives false
positives (`close_full_after_tps` appears as a keyword-argument name in nine
handler modules) and the codebase reaches modules through aliases constantly --
`as db_module`, `as _cdb`, `as sched`, `as _et`, `as core_db`.

Usage:
    python tools/refactor_audit/orphan_detector.py            # human report
    python tools/refactor_audit/orphan_detector.py --json     # machine readable
    python tools/refactor_audit/orphan_detector.py --check    # CI: exit 1 on new orphans
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
# core/ was fully dissolved by the restructure; the glob over it returns
# nothing, which keeps every core_*-scoped check vacuously green while the
# history-mode tools (divergence_detector) can still be pointed at old paths.
CORE_DIR = REPO_ROOT / "forex_trader" / "core"
ALLOWLIST_PATH = Path(__file__).parent / "orphan_allowlist.json"

# Directories that are not production code. Tests are excluded on purpose: a
# module reachable only from its own test is exactly what we're hunting for.
EXCLUDED_DIRS = {"tests", ".git", ".venv", "venv", "docs", "installer", "mql5",
                 "__pycache__", "tools"}


def production_files() -> list[Path]:
    return [
        p for p in REPO_ROOT.rglob("*.py")
        if not any(part in EXCLUDED_DIRS for part in p.relative_to(REPO_ROOT).parts)
    ]


def module_stem(dotted: str) -> str:
    """'backend.src.services.trading.open_trade' -> 'core_open_trade'."""
    return dotted.rsplit(".", 1)[-1]


class UsageCollector(ast.NodeVisitor):
    """Records every (module_stem, symbol) pair a file could reach at runtime.

    Two shapes matter:
      from forex_trader.core.core_x import foo      -> ('core_x', 'foo')
      from forex_trader.core import core_x as cx
      ... cx.foo(...)                               -> ('core_x', 'foo')
    """

    def __init__(self) -> None:
        self.used: set[tuple[str, str]] = set()
        # alias name -> module stem, e.g. {'cx': 'core_x', 'db_module': 'database'}
        self.aliases: dict[str, str] = {}

    def visit_Import(self, node: ast.Import) -> None:
        for a in node.names:
            stem = module_stem(a.name)
            self.aliases[a.asname or stem] = stem
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        base = node.module or ""
        for a in node.names:
            if a.name == "*":
                # Star-import: we can't tell what's used. Mark the whole module
                # as reachable so we never report a false orphan.
                self.used.add((module_stem(base), "*"))
                continue
            # `from forex_trader.core import core_x` -- the name IS a module.
            if a.name.startswith("core_") or a.name in ("database", "engine"):
                self.aliases[a.asname or a.name] = a.name
            # `from forex_trader.core.core_x import foo` -- the name is a symbol.
            if base:
                self.used.add((module_stem(base), a.name))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name):
            stem = self.aliases.get(node.value.id)
            if stem:
                self.used.add((stem, node.attr))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        # getattr(mod, "name") defeats the Attribute walk. Treat a literal
        # second argument as a use; a non-literal one marks the whole module
        # reachable, since we can't resolve it.
        if isinstance(node.func, ast.Name) and node.func.id == "getattr" and node.args:
            target = node.args[0]
            if isinstance(target, ast.Name) and (stem := self.aliases.get(target.id)):
                if len(node.args) > 1 and isinstance(node.args[1], ast.Constant) \
                        and isinstance(node.args[1].value, str):
                    self.used.add((stem, node.args[1].value))
                else:
                    self.used.add((stem, "*"))
        self.generic_visit(node)


def collect_usage(files: list[Path]) -> dict[tuple[str, str], list[str]]:
    """Maps (module_stem, symbol) -> the files that reach it."""
    usage: dict[tuple[str, str], list[str]] = {}
    for path in files:
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError) as exc:
            print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
            continue
        collector = UsageCollector()
        collector.visit(tree)
        rel = str(path.relative_to(REPO_ROOT))
        for key in collector.used:
            usage.setdefault(key, []).append(rel)
    return usage


def public_functions(path: Path) -> list[tuple[str, int, int]]:
    """Public top-level defs in a module -> (name, lineno, loc)."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and not node.name.startswith("_"):
            loc = (node.end_lineno or node.lineno) - node.lineno + 1
            out.append((node.name, node.lineno, loc))
    return out


def find_orphans() -> list[dict]:
    usage = collect_usage(production_files())
    orphans = []
    for path in sorted(CORE_DIR.glob("core_*.py")):
        stem = path.stem
        if (stem, "*") in usage:
            continue  # star-imported somewhere; can't prove anything is dead
        for name, lineno, loc in public_functions(path):
            callers = [
                f for f in usage.get((stem, name), [])
                if f != str(path.relative_to(REPO_ROOT))
            ]
            if not callers:
                orphans.append({
                    "module": stem,
                    "function": name,
                    "file": str(path.relative_to(REPO_ROOT)),
                    "line": lineno,
                    "loc": loc,
                })
    return sorted(orphans, key=lambda o: -o["loc"])


def load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.exists():
        return set()
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return {entry["id"] for entry in data.get("allowed", [])}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if any orphan is not in the allowlist")
    args = parser.parse_args()

    orphans = find_orphans()

    if args.json:
        print(json.dumps(orphans, indent=2))
    else:
        if not orphans:
            print("No orphaned functions found.")
        else:
            total = sum(o["loc"] for o in orphans)
            print(f"{len(orphans)} orphaned public functions, {total} LOC of dead code:\n")
            print(f"{'LOC':>5}  {'MODULE::FUNCTION':<58}  LOCATION")
            for o in orphans:
                ident = f"{o['module']}::{o['function']}"
                print(f"{o['loc']:>5}  {ident:<58}  {o['file']}:{o['line']}")

    if args.check:
        allowed = load_allowlist()
        unexpected = [o for o in orphans
                      if f"{o['module']}::{o['function']}" not in allowed]
        if unexpected:
            print(f"\nFAIL: {len(unexpected)} orphan(s) not in "
                  f"{ALLOWLIST_PATH.relative_to(REPO_ROOT)}:", file=sys.stderr)
            for o in unexpected:
                print(f"  {o['module']}::{o['function']} "
                      f"({o['file']}:{o['line']})", file=sys.stderr)
            print("\nAn extracted function nothing calls is dead code. Either wire it "
                  "in, delete it, or record it in the allowlist with a reason.",
                  file=sys.stderr)
            return 1
        print(f"\nOK: {len(orphans)} orphan(s), all allowlisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
