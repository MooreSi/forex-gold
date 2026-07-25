"""Shared AST normalisation for comparing an extracted function against the
inline method it was extracted from.

Extraction rewrites a bound method into a free function: `self` disappears from
the signature, collaborator state that used to be `self._bridge` arrives as a
parameter, and the shared `CloseTradeContext` is passed as `ctx`. None of that
changes behaviour, so a comparison that flags it is useless. Normalisation
strips exactly those mechanical differences and leaves everything else --
including a dropped state assignment or a missing alert -- visible.

Comparison is on the AST, never on text: the extraction already produced
Unicode em-dash and arrow variants that defeated string matching once
(docs/todo/refactor/core-engine-wiring/PROGRESS.md).
"""
from __future__ import annotations

import ast

# Receiver names that vanish or get renamed during extraction. `self.x` in the
# method and a bare `x` parameter in the extracted function are the same value.
TRANSPARENT_RECEIVERS = {"self", "ctx", "context"}


def _public(name: str) -> str:
    """`_record_close` -> `record_close`. Dunders are left alone."""
    if name.startswith("__"):
        return name
    return name.lstrip("_") or name


class _Normaliser(ast.NodeTransformer):
    def visit_Attribute(self, node: ast.Attribute) -> ast.AST:
        self.generic_visit(node)
        if isinstance(node.value, ast.Name) and node.value.id in TRANSPARENT_RECEIVERS:
            # self._bridge -> _bridge, ctx.tp_cache -> tp_cache
            return ast.copy_location(
                ast.Name(id=_public(node.attr), ctx=node.ctx), node)
        return node

    def visit_Name(self, node: ast.Name) -> ast.AST:
        # After the rewrite above, the method's `self._bridge` reads as `_bridge`
        # while the extracted function's parameter is `bridge`; likewise
        # `self._record_close` vs `record_close`. Dropping the private prefix
        # collapses that mechanical churn so the diff shows only the statements
        # that actually differ. Names are cosmetic here -- statement_shape()
        # ignores them entirely -- so this cannot mask a truncation.
        return ast.copy_location(ast.Name(id=_public(node.id), ctx=node.ctx), node)

    def visit_Constant(self, node: ast.Constant) -> ast.AST:
        # Multi-line SQL and log strings get re-indented when a method body is
        # dedented into a module-level function. That changes the literal's
        # internal whitespace and nothing else. Collapsing whitespace runs
        # keeps those out of the diff.
        #
        # This is deliberately lossy: a change that ONLY alters runs of spaces
        # inside a string becomes invisible. No SQL and no Telegram message in
        # this codebase depends on that, and the alternative is a diff so noisy
        # nobody reads it.
        if isinstance(node.value, str) and ("\n" in node.value or "  " in node.value):
            collapsed = " ".join(node.value.split())
            return ast.copy_location(ast.Constant(value=collapsed, kind=node.kind), node)
        return node

    def visit_arguments(self, node: ast.arguments) -> ast.AST:
        # Signatures differ by construction: the method takes (self, ...), the
        # extracted function takes (bridge, tp_cache, ...). Erase them.
        return ast.arguments(
            posonlyargs=[], args=[], vararg=None, kwonlyargs=[],
            kw_defaults=[], kwarg=None, defaults=[],
        )


def strip_docstring(body: list[ast.stmt]) -> list[ast.stmt]:
    if body and isinstance(body[0], ast.Expr) and \
            isinstance(body[0].value, ast.Constant) and \
            isinstance(body[0].value.value, str):
        return body[1:]
    return body


def normalise(func: ast.FunctionDef | ast.AsyncFunctionDef) -> ast.Module:
    """Returns a Module holding the function's body, mechanically normalised."""
    clone = ast.parse(ast.unparse(func)).body[0]
    clone.body = strip_docstring(clone.body)
    clone.name = "_"
    normalised = _Normaliser().visit(clone)
    ast.fix_missing_locations(normalised)
    return ast.Module(body=[normalised], type_ignores=[])


def normalised_source(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str:
    return ast.unparse(normalise(func))


def statement_shape(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """The sequence of statement node types, flattened depth-first.

    A truncation -- the core_run_tp_ladder failure, where a `current_sl = new_sl`
    assignment and a Telegram alert were silently dropped -- changes this
    sequence even when the surviving text matches perfectly.
    """
    module = normalise(func)
    return [type(n).__name__ for n in ast.walk(module)
            if isinstance(n, ast.stmt)]


def decorator_names(func: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    """Decorators are compared separately.

    The database split's extraction script read functions by `lineno` and so
    dropped `@contextmanager` from `db()` -- the only decorated top-level
    function in the file (core-database-migration/PROGRESS.md:29). Nothing in a
    body comparison would have caught it.
    """
    out = []
    for d in func.decorator_list:
        if isinstance(d, ast.Name):
            out.append(d.id)
        elif isinstance(d, ast.Attribute):
            out.append(d.attr)
        elif isinstance(d, ast.Call):
            out.append(ast.unparse(d.func))
        else:
            out.append(ast.unparse(d))
    return out


def find_function(tree: ast.AST, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef | None:
    """Finds a top-level function or a method anywhere in the tree, by name."""
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    return None
