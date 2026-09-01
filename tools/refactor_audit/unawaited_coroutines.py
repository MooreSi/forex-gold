"""A coroutine created and never awaited does nothing, quietly.

Calling an `async def` without `await` builds a coroutine object and drops it.
No error, no work done — just a `RuntimeWarning` on stderr that nobody reads in
a running app. The operation simply never happens.

This is easy to introduce by changing a function from sync to async: every
`await`ed call site keeps working, and any bare call site silently stops
working. That happened here on 2026-09-01 — `_refresh_cb_badge` was made async
so a timer would stop blocking the event loop, and the one-off call that
populates the badge on first render became a no-op.

**Only bare NAME calls are reported.** `app.shutdown()` is NiceGUI's own,
synchronous, and shares a name with three async `shutdown` methods in this
tree; matching on attribute calls would report it and two others as false
positives, and a gate with false positives gets switched off.
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".claude", "node_modules"}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    name: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: {self.name}() is async and is not awaited"


def _py_files(roots):
    for root in roots:
        root = pathlib.Path(root)
        if root.is_file():
            yield root
            continue
        for p in sorted(root.rglob("*.py")):
            if not EXCLUDED_DIRS & set(p.parts):
                yield p


def scan(roots) -> list:
    trees: dict = {}
    async_names: set = set()
    sync_names: set = set()
    for p in _py_files(roots):
        try:
            tree = ast.parse(p.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        trees[p] = tree
        for n in ast.walk(tree):
            if isinstance(n, ast.AsyncFunctionDef):
                async_names.add(n.name)
            elif isinstance(n, ast.FunctionDef):
                sync_names.add(n.name)

    # A name defined BOTH ways somewhere in the tree cannot be judged from the
    # call site alone, so it is left alone rather than guessed at.
    only_async = async_names - sync_names

    findings: list = []
    for p, tree in trees.items():
        rel = str(p).replace("\\", "/")
        for n in ast.walk(tree):
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Call)):
                continue
            func = n.value.func
            if isinstance(func, ast.Name) and func.id in only_async:
                findings.append(Finding(rel, n.lineno, func.id))
    return findings


if __name__ == "__main__":
    import sys
    hits = scan(sys.argv[1:] or ["backend", "frontend", "tools"])
    for h in hits:
        print(h)
    print(f"\n{len(hits)} un-awaited coroutine call(s)")
    sys.exit(1 if hits else 0)
