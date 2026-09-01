"""A callback defined in a loop that reads the loop's variables later.

Python closures capture by reference, not by value. A function defined inside a
loop and called *after* the loop sees the LAST iteration's values, not the ones
present when it was defined.

In a UI that renders one row per item, that means every row's button operates
on the last row. Found on 2026-09-01 in the Pending Signals editor: `save_edit`
captured the signal id correctly and none of its fourteen input widgets, so
Save on any row wrote the last row's entry, stop loss and targets onto its own
signal -- and `update_signal` pushes SL/TP through to an open trade.

The idiom that fixes it is already used elsewhere in the same codebase:

    async def do_partial(tid=trade_id, pl_inp=partial_lots):   # captured
    async def save_edit(sid=signal_id):                        # was not

Not every hit is a bug. A closure invoked within the same iteration -- a
`sorted(key=...)`, a function awaited on the next line -- never outlives the
values it reads, and cannot be told apart from the dangerous kind by reading
the definition alone. Those are listed in ALLOWED with a reason each, rather
than being silently filtered, so the list stays short and each entry has to be
justified.
"""
from __future__ import annotations

import ast
import pathlib
from dataclasses import dataclass

EXCLUDED_DIRS = {".git", ".venv", "__pycache__", ".claude", "node_modules"}

# Closures that read a loop variable but are CALLED within the same iteration,
# so late binding cannot reach them. Each needs a reason.
ALLOWED = {
    "backend/src/services/analytics/trade_history_repo.py::<lambda>":
        "sorted(key=...) -- called by sorted() before the iteration ends",
    "backend/src/services/breakout_signal/breakout_signal_service.py::_fetch_mt5_close":
        "awaited on the next line, inside the same iteration",
    "backend/src/services/test_signal/test_signal_service.py::_fetch_mt5_close":
        "awaited on the next line, inside the same iteration",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    func: str
    names: tuple

    @property
    def key(self) -> str:
        return f"{self.path}::{self.func}"

    def __str__(self) -> str:
        return (f"{self.path}:{self.line}: {self.func}() reads "
                f"{list(self.names)} from its loop without capturing them")


def _target_names(node) -> set:
    out: set = set()
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for e in node.elts:
            out |= _target_names(e)
    elif isinstance(node, ast.Starred):
        out |= _target_names(node.value)
    return out


def _bound_in(node) -> set:
    """Names bound inside a scope, so they are locals there and not captures."""
    out: set = set()
    for n in ast.walk(node):
        if isinstance(n, ast.Assign):
            for t in n.targets:
                out |= _target_names(t)
        elif isinstance(n, (ast.AnnAssign, ast.AugAssign, ast.NamedExpr)):
            out |= _target_names(n.target)
        elif isinstance(n, (ast.For, ast.AsyncFor)):
            out |= _target_names(n.target)
        elif isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if item.optional_vars is not None:
                    out |= _target_names(item.optional_vars)
        elif isinstance(n, ast.ExceptHandler) and n.name:
            out.add(n.name)
    return out


def _py_files(roots):
    for root in roots:
        root = pathlib.Path(root)
        if root.is_file():
            yield root
            continue
        for p in sorted(root.rglob("*.py")):
            if not EXCLUDED_DIRS & set(p.parts):
                yield p


def scan(roots, repo_root=None) -> list:
    repo_root = pathlib.Path(repo_root) if repo_root else pathlib.Path(".")
    findings: list = []
    for path in _py_files(roots):
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        try:
            rel = str(path.relative_to(repo_root))
        except ValueError:
            rel = str(path)
        rel = rel.replace("\\", "/")

        for loop in ast.walk(tree):
            if not isinstance(loop, (ast.For, ast.AsyncFor)):
                continue
            loop_vars = _target_names(loop.target)
            for stmt in loop.body:
                for n in ast.walk(stmt):
                    if isinstance(n, ast.Assign):
                        for t in n.targets:
                            loop_vars |= _target_names(t)

            for node in ast.walk(loop):
                if not isinstance(node, (ast.Lambda, ast.FunctionDef,
                                         ast.AsyncFunctionDef)):
                    continue
                args = node.args
                captured = {a.arg for a in
                            args.posonlyargs + args.args + args.kwonlyargs}
                body = node.body if isinstance(node.body, list) else [node.body]
                local: set = set()
                for stmt in body:
                    local |= _bound_in(stmt)
                used = {x.id for stmt in body for x in ast.walk(stmt)
                        if isinstance(x, ast.Name) and isinstance(x.ctx, ast.Load)}
                late = (used & loop_vars) - captured - local
                if late:
                    findings.append(Finding(
                        rel, node.lineno, getattr(node, "name", "<lambda>"),
                        tuple(sorted(late))))
    return [f for f in findings if f.key not in ALLOWED]


if __name__ == "__main__":
    import sys
    roots = sys.argv[1:] or ["backend", "frontend"]
    hits = scan(roots)
    for h in hits:
        print(h)
    print(f"\n{len(hits)} late-binding capture(s)")
    sys.exit(1 if hits else 0)
