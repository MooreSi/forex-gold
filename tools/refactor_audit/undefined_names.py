"""Find names a module uses but never defines, imports or receives.

The bug class this exists for: **a split leaves a function's dependency
behind.** Code is moved to a new module, its callers are updated, the suite
goes green -- and the moved function still refers to a constant that stayed in
the original file. Nothing fails until that exact line runs in production.

This repo has had it four times. `docs/todo/bugs/010` (undefined `ap` in
test_panel) and `011` (undefined `_SIGNAL_GEN_SYSTEM` in ai_trade_analysis)
are two. The 2026-08-30 cluster splits were the other two, and they shipped
through `tools.checks all` 8/8 because nothing looks for this and the coverage
in those files was 17-27%.

Why not pyflakes: it would do this and more, but it is not a declared
dependency of this project and adding a runtime/CI dependency is not a
decision this scanner gets to make. This implementation is deliberately
narrow -- undefined names only -- and was validated by comparing its output
against pyflakes 3.4.0 across the whole tree (see
tests/refactor/test_undefined_names.py).

Scope handling, and the limits worth knowing:

  * `from x import *` makes a module unanalysable, so it is skipped entirely
    rather than guessed at.
  * Class bodies are their own scope and deliberately do NOT leak into methods
    defined inside them -- that is Python's actual rule.
  * Comprehensions and lambdas get their own scope.
  * `global` / `nonlocal` bind where they say they do.
  * A name is only reported when it is not local, not enclosing, not
    module-level, and not a builtin.
"""
from __future__ import annotations

import ast
import builtins
import pathlib
from dataclasses import dataclass

EXCLUDED_DIRS = {
    ".git", ".venv", "__pycache__", ".claude", "node_modules", ".pytest_cache",
}

_BUILTINS = set(dir(builtins)) | {
    "__file__", "__name__", "__doc__", "__package__", "__spec__",
    "__loader__", "__builtins__", "__debug__", "__path__",
}


@dataclass(frozen=True)
class Finding:
    path: str
    line: int
    name: str

    def __str__(self) -> str:
        return f"{self.path}:{self.line}: undefined name {self.name!r}"


def _target_names(node: ast.AST) -> set:
    """Every name bound by an assignment target, however nested."""
    out: set = set()
    if isinstance(node, ast.Name):
        out.add(node.id)
    elif isinstance(node, (ast.Tuple, ast.List)):
        for elt in node.elts:
            out |= _target_names(elt)
    elif isinstance(node, ast.Starred):
        out |= _target_names(node.value)
    # Attribute and Subscript targets bind nothing new.
    return out


def _bound_here(body, *, include_nested_defs=True) -> set:
    """Names bound directly in this scope's body, not descending into nested
    function or class scopes (their names belong to them, not to us)."""
    bound: set = set()

    def walk(node, top=False):
        for child in ast.iter_child_nodes(node):
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if include_nested_defs:
                    bound.add(child.name)
                continue                      # its body is its own scope
            if isinstance(child, ast.ClassDef):
                if include_nested_defs:
                    bound.add(child.name)
                continue
            if isinstance(child, (ast.Lambda, ast.ListComp, ast.SetComp,
                                  ast.DictComp, ast.GeneratorExp)):
                continue                      # own scope too
            if isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    if alias.name == "*":
                        continue
                    bound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(child, ast.Assign):
                for t in child.targets:
                    bound.update(_target_names(t))
            elif isinstance(child, (ast.AnnAssign, ast.AugAssign)):
                bound.update(_target_names(child.target))
            elif isinstance(child, ast.NamedExpr):
                bound.update(_target_names(child.target))
            elif isinstance(child, (ast.For, ast.AsyncFor)):
                bound.update(_target_names(child.target))
            elif isinstance(child, (ast.With, ast.AsyncWith)):
                for item in child.items:
                    if item.optional_vars is not None:
                        bound.update(_target_names(item.optional_vars))
            elif isinstance(child, ast.ExceptHandler):
                if child.name:
                    bound.add(child.name)
            elif isinstance(child, (ast.Global, ast.Nonlocal)):
                bound.update(child.names)
            elif isinstance(child, (ast.Match,)):
                bound.update(n.id for n in ast.walk(child)
                             if isinstance(n, ast.Name)
                             and isinstance(n.ctx, ast.Store))
            walk(child)

    # One synthetic parent, so `walk` sees the top-level statements themselves
    # as children. Calling walk(stmt) per statement inspects each statement's
    # CHILDREN and skips the statement -- which silently binds nothing at
    # module level.
    walk(ast.Module(body=list(body), type_ignores=[]))
    return bound


def _arg_names(args: ast.arguments) -> set:
    out = {a.arg for a in args.posonlyargs + args.args + args.kwonlyargs}
    if args.vararg:
        out.add(args.vararg.arg)
    if args.kwarg:
        out.add(args.kwarg.arg)
    return out


def _has_star_import(tree: ast.AST) -> bool:
    return any(
        isinstance(n, ast.ImportFrom) and any(a.name == "*" for a in n.names)
        for n in ast.walk(tree)
    )


def _check_scope(node, visible: set, findings: list, path: str) -> None:
    """`visible` is every name reachable from here (module + enclosing +
    local). Class bodies are handled by their caller."""
    for child in ast.iter_child_nodes(node):
        if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            # Decorators and defaults evaluate in THIS scope.
            for dec in getattr(child, "decorator_list", []):
                _check_expr(dec, visible, findings, path)
            _check_defaults(child.args, visible, findings, path)
            # Annotations. Under `from __future__ import annotations` these
            # are never evaluated, so an undefined name here does not crash --
            # but it still means an import was left behind by whoever moved
            # the code, which is exactly the signal this scanner is for. It
            # also breaks typing.get_type_hints() for anyone who asks.
            _check_annotations(child, visible, findings, path)
            inner = set(visible)
            inner |= _arg_names(child.args)
            body = child.body if isinstance(child.body, list) else [child.body]
            inner |= _bound_here(body)
            _check_scope_body(body, inner, findings, path)
            continue
        if isinstance(child, ast.ClassDef):
            for dec in child.decorator_list:
                _check_expr(dec, visible, findings, path)
            for base in child.bases:
                _check_expr(base, visible, findings, path)
            # A class body sees the enclosing scope, but methods inside it do
            # NOT see the class body's names -- so the class body's own
            # bindings are visible here and dropped for nested scopes.
            body_names = _bound_here(child.body)
            _check_scope_body(child.body, visible | body_names, findings, path,
                              hide_from_nested=body_names)
            continue
        if isinstance(child, (ast.ListComp, ast.SetComp, ast.DictComp,
                              ast.GeneratorExp)):
            inner = set(visible)
            for gen in child.generators:
                _check_expr(gen.iter, inner, findings, path)
                inner |= _target_names(gen.target)
                for cond in gen.ifs:
                    _check_expr(cond, inner, findings, path)
            for part in ("elt", "key", "value"):
                if hasattr(child, part):
                    _check_expr(getattr(child, part), inner, findings, path)
            continue
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
            if child.id not in visible and child.id not in _BUILTINS:
                findings.append(Finding(path, child.lineno, child.id))
            continue
        _check_scope(child, visible, findings, path)


def _check_scope_body(body, visible, findings, path, hide_from_nested=None):
    holder = ast.Module(body=list(body), type_ignores=[])
    if hide_from_nested:
        # Nested defs inside a class body cannot see the class attributes.
        for stmt in body:
            if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
                _check_scope(ast.Module(body=[stmt], type_ignores=[]),
                             visible - hide_from_nested, findings, path)
        rest = [s for s in body
                if not isinstance(s, (ast.FunctionDef, ast.AsyncFunctionDef,
                                      ast.ClassDef))]
        holder = ast.Module(body=rest, type_ignores=[])
    _check_scope(holder, visible, findings, path)


def _check_expr(node, visible, findings, path) -> None:
    _check_scope(ast.Module(body=[ast.Expr(value=node)], type_ignores=[]),
                 visible, findings, path)


def _check_annotations(fn, visible, findings, path) -> None:
    if isinstance(fn, ast.Lambda):
        return                     # a lambda cannot carry annotations
    args = fn.args
    for a in (args.posonlyargs + args.args + args.kwonlyargs
              + [x for x in (args.vararg, args.kwarg) if x]):
        if a.annotation is not None:
            _check_expr(a.annotation, visible, findings, path)
    if fn.returns is not None:
        _check_expr(fn.returns, visible, findings, path)


def _check_defaults(args: ast.arguments, visible, findings, path) -> None:
    for d in list(args.defaults) + [d for d in args.kw_defaults if d]:
        _check_expr(d, visible, findings, path)


def check_file(path: pathlib.Path, rel: str) -> list:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    if _has_star_import(tree):
        return []
    module_names = _bound_here(tree.body)
    findings: list = []
    _check_scope(tree, module_names, findings, rel)
    # De-duplicate identical (line, name) pairs.
    seen, out = set(), []
    for f in findings:
        key = (f.line, f.name)
        if key not in seen:
            seen.add(key)
            out.append(f)
    return out


def scan(roots) -> list:
    findings: list = []
    for root in roots:
        root = pathlib.Path(root)
        if root.is_file():
            findings += check_file(root, str(root))
            continue
        for p in sorted(root.rglob("*.py")):
            if EXCLUDED_DIRS & set(p.parts):
                continue
            findings += check_file(p, str(p).replace("\\", "/"))
    return findings


if __name__ == "__main__":
    import sys
    hits = scan(sys.argv[1:] or ["backend", "frontend", "tools"])
    for h in hits:
        print(h)
    print(f"\n{len(hits)} undefined name(s)")
    sys.exit(1 if hits else 0)
