#!/usr/bin/env python3
"""Finds whole modules that nothing imports — dead code at the file level.

The older `orphan_detector.py` hunts dead *functions* inside `forex_trader/core/
core_*.py`. That directory no longer exists, so it globs nothing and is
vacuously green (testing review 2026-08-08, H1) — the exact "guardrail scans a
deleted directory and prints all-good for months" failure the golden rules
exist to prevent. It is kept only as a shared helper library for other gates.

This detector answers the question that actually matters for this codebase's
dead code: **which production modules are never reached, transitively, from the
app's entrypoints?** It builds the intra-repo import graph by AST, walks it from
the entrypoints, and reports every unreached module.

Two design points that keep it honest:

  * It FAILS CLOSED. If an entrypoint or a scan root has gone missing (renamed,
    deleted), it errors loudly instead of scanning nothing and passing. A gate
    that cannot find what it audits must never report success.

  * Package `__init__.py` files are not reported. An empty or scaffolding
    package-init is not dead code; genuine package-level death shows up as its
    submodules being orphaned. A reached submodule implies its ancestor
    packages are reached (Python runs their __init__ on import).

Known orphans that are debt-we-haven't-deleted-yet live in
`orphan_module_allowlist.json`. An allowlist entry is a debt record, not an
approval; `--check` fails only on orphans NOT in it.

Usage:
    python -m tools.refactor_audit.orphan_modules            # human report
    python -m tools.refactor_audit.orphan_modules --json     # machine readable
    python -m tools.refactor_audit.orphan_modules --check    # CI: exit 1 on new orphans
    python -m tools.refactor_audit.orphan_modules --update-allowlist  # rewrite the ledger
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
ALLOWLIST_PATH = Path(__file__).parent / "orphan_module_allowlist.json"

# Directories that are not production code. Tests are excluded on purpose: a
# module reachable only from its own test is exactly what we're hunting.
EXCLUDED_DIRS = {"tests", ".git", ".venv", "venv", "docs", "installer", "mql5",
                 "__pycache__", "tools", "latest_logs", "archived_logs",
                 "scratchpad", "build", "dist",
                 "notebooks"}  # offline data-science lab: standalone scripts, never imported by the app

# The roots of the production import graph. Every module the running app (or a
# shipped standalone script) can reach must trace back to one of these. A name
# here that does not resolve to a file is a hard error, not a warning.
ENTRYPOINTS = (
    "run",                    # the launcher
    "mt5_bridge",             # the Wine-side bridge process
    "backend.src.app",        # the composition root
    "frontend.app",           # the UI, registers every page
    "signal_generator_report",  # shipped standalone report CLI (has __main__)
)


def production_modules(root: Path) -> dict[str, Path]:
    """Map dotted module name -> file for every production .py under `root`."""
    out: dict[str, Path] = {}
    for p in root.rglob("*.py"):
        rel = p.relative_to(root)
        if any(part in EXCLUDED_DIRS for part in rel.parts):
            continue
        parts = list(rel.with_suffix("").parts)
        if parts and parts[-1] == "__init__":
            parts = parts[:-1]
        dotted = ".".join(parts)
        if dotted:
            out[dotted] = p
    return out


def _package_parts(dotted: str, path: Path, root: Path) -> list[str]:
    """The package (parent) parts of a module, for resolving relative imports."""
    rel = path.relative_to(root).with_suffix("")
    parts = list(rel.parts)
    if parts and parts[-1] == "__init__":
        parts = parts[:-1]
        return parts  # a package's own parts are its package context
    return parts[:-1]


def _imported_names(path: Path, pkg_parts: list[str]) -> set[str]:
    """Every module string this file imports (absolute form, relatives resolved)."""
    names: set[str] = set()
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError) as exc:
        print(f"warning: could not parse {path}: {exc}", file=sys.stderr)
        return names
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                names.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                # `from . import x` / `from ..pkg import y`
                base = pkg_parts[: len(pkg_parts) - (node.level - 1)] if node.level > 1 else pkg_parts
                mod_parts = base + ([node.module] if node.module else [])
                mod = ".".join(p for p in mod_parts if p)
            else:
                mod = node.module or ""
            if mod:
                names.add(mod)
                for a in node.names:
                    # `from pkg import submodule` — the name may itself be a module
                    names.add(f"{mod}.{a.name}")
    return names


def build_graph(module_map: dict[str, Path], root: Path) -> dict[str, set[str]]:
    """module -> set of in-repo modules it imports (longest-prefix resolved)."""
    graph: dict[str, set[str]] = {}
    for dotted, path in module_map.items():
        pkg = _package_parts(dotted, path, root)
        edges: set[str] = set()
        for name in _imported_names(path, pkg):
            parts = name.split(".")
            for i in range(len(parts), 0, -1):
                cand = ".".join(parts[:i])
                if cand in module_map:
                    edges.add(cand)
                    break
        graph[dotted] = edges
    return graph


def _ancestors(dotted: str, module_map: dict[str, Path]) -> list[str]:
    """Ancestor packages of a module that exist as their own modules (__init__)."""
    parts = dotted.split(".")
    return [".".join(parts[:i]) for i in range(1, len(parts)) if ".".join(parts[:i]) in module_map]


def reachable(module_map: dict[str, Path], graph: dict[str, set[str]],
              entrypoints: tuple[str, ...]) -> set[str]:
    seen: set[str] = set()
    stack = list(entrypoints)
    while stack:
        m = stack.pop()
        if m in seen:
            continue
        seen.add(m)
        # Importing a module runs its ancestor packages' __init__.
        for anc in _ancestors(m, module_map):
            if anc not in seen:
                stack.append(anc)
        for e in graph.get(m, ()):
            if e not in seen:
                stack.append(e)
    return seen


def _loc(path: Path) -> int:
    return len(path.read_text(encoding="utf-8").splitlines())


def find_orphans(root: Path = REPO_ROOT,
                 entrypoints: tuple[str, ...] = ENTRYPOINTS) -> list[dict]:
    """Unreached, non-__init__ production modules under `root`.

    Raises SystemExit if an entrypoint does not resolve to a file — the gate
    must fail closed rather than audit an empty graph.
    """
    if not root.exists():
        raise SystemExit(f"orphan-modules: scan root {root} does not exist")
    module_map = production_modules(root)
    missing = [e for e in entrypoints if e not in module_map]
    if missing:
        raise SystemExit(
            "orphan-modules: entrypoint(s) not found: "
            f"{', '.join(missing)}\nThe import graph has no roots to walk from; "
            "refusing to report everything (or nothing) as an orphan."
        )
    graph = build_graph(module_map, root)
    reached = reachable(module_map, graph, entrypoints)
    orphans = []
    for dotted, path in module_map.items():
        if dotted in reached:
            continue
        if path.name == "__init__.py":
            continue  # package scaffolding, not dead code
        orphans.append({
            "module": dotted,
            "file": path.relative_to(root).as_posix(),
            "loc": _loc(path),
        })
    return sorted(orphans, key=lambda o: -o["loc"])


def load_allowlist() -> set[str]:
    if not ALLOWLIST_PATH.exists():
        return set()
    data = json.loads(ALLOWLIST_PATH.read_text(encoding="utf-8"))
    return {entry["module"] for entry in data.get("allowed", [])}


def _write_allowlist(orphans: list[dict]) -> None:
    ALLOWLIST_PATH.write_text(json.dumps({
        "_comment": [
            "Whole modules that nothing imports. Each entry is dead code we have",
            "decided not to delete YET -- a debt record, not an approval. When a",
            "module is deleted, remove its entry; when new dead code appears, the",
            "gate fails until it is deleted or (rarely) recorded here with a reason.",
        ],
        "allowed": [
            {"module": o["module"], "file": o["file"], "loc": o["loc"],
             "reason": "dead — scheduled for deletion (stage1 phase3/010)"}
            for o in orphans
        ],
    }, indent=2) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    parser.add_argument("--check", action="store_true",
                        help="exit 1 if any orphan is not in the allowlist")
    parser.add_argument("--update-allowlist", action="store_true",
                        help="rewrite the allowlist to exactly today's orphans")
    args = parser.parse_args()

    orphans = find_orphans()

    if args.update_allowlist:
        _write_allowlist(orphans)
        print(f"Orphan-module allowlist written: {len(orphans)} module(s), "
              f"{sum(o['loc'] for o in orphans)} LOC.")
        return 0

    if args.json:
        print(json.dumps(orphans, indent=2))
    elif not orphans:
        print("No orphaned modules found.")
    else:
        total = sum(o["loc"] for o in orphans)
        print(f"{len(orphans)} orphaned modules, {total} LOC nothing imports:\n")
        print(f"{'LOC':>6}  MODULE")
        for o in orphans:
            print(f"{o['loc']:>6}  {o['module']}  ({o['file']})")

    if args.check:
        allowed = load_allowlist()
        unexpected = [o for o in orphans if o["module"] not in allowed]
        stale = sorted(allowed - {o["module"] for o in orphans})
        if unexpected:
            print(f"\nFAIL: {len(unexpected)} orphan module(s) not in "
                  f"{ALLOWLIST_PATH.name}:", file=sys.stderr)
            for o in unexpected:
                print(f"  {o['module']} ({o['file']}, {o['loc']} LOC)", file=sys.stderr)
            print("\nA module nothing imports is dead code. Delete it, wire it in, "
                  "or record it in the allowlist with a reason.", file=sys.stderr)
            return 1
        if stale:
            print(f"\nFAIL: {len(stale)} allowlist entr(y/ies) no longer orphaned "
                  f"(deleted or re-wired?) — remove them from {ALLOWLIST_PATH.name}:",
                  file=sys.stderr)
            for m in stale:
                print(f"  {m}", file=sys.stderr)
            return 1
        print(f"\nOK: {len(orphans)} orphan module(s), all allowlisted.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
