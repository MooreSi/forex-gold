"""Finds the controller operations that are plain forwarders, and how to reach
the service function each one forwards to.

`docs/system/rules/30-architecture.md` defines a controller as *"a flat
`<name>_controller.py` that names an operation and forwards it to one service —
no loops, no merges, no formatting, no fallbacks"*. That sentence is a testable
claim about 227 of the 254 operations in the layer, and this module is what
makes it testable: it reads each function, works out which service function it
calls, and hands the pair to `test_controller_forwarding.py` to exercise.

It is deliberately conservative. A function it cannot read as a plain forwarder
is reported as complex rather than guessed at, and the sweep asserts that set is
exactly the one recorded — so a controller that grows a branch shows up as a new
entry to justify instead of quietly dropping out of the sweep.
"""
from __future__ import annotations

import ast
import importlib
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

REPO = Path(__file__).resolve().parents[2]
CONTROLLER_DIR = REPO / "backend" / "src" / "controllers"


@dataclass(frozen=True)
class Forwarder:
    """One controller operation and the service call it makes."""

    module: str          # "trading_controller"
    name: str            # "get_risk_settings"
    alias: str           # the name the call is made on, e.g. "_risk"
    attr: str            # the attribute called on it, e.g. "get"
    is_async: bool
    returns: bool        # False for the fire-and-forget writes
    local_import: Optional[str]   # source of the in-function import, if any
    on_parameter: bool = False    # the call is made on an injected object

    @property
    def qualname(self) -> str:
        return f"{self.module}.{self.name}"


def _significant(fn: ast.AST) -> list[ast.stmt]:
    """The function's body without its docstring."""
    return [n for n in fn.body
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant))]


def _import_source(stmt: ast.stmt, source: str) -> str:
    return ast.get_source_segment(source, stmt) or ""


def _parameter_names(fn: ast.AST) -> set[str]:
    a = fn.args
    names = {p.arg for p in list(a.args) + list(a.posonlyargs) + list(a.kwonlyargs)}
    if a.vararg:
        names.add(a.vararg.arg)
    if a.kwarg:
        names.add(a.kwarg.arg)
    return names


def _passes_only_its_own_arguments(call: ast.Call, fn: ast.AST) -> bool:
    params = _parameter_names(fn)
    for arg in call.args:
        node = arg.value if isinstance(arg, ast.Starred) else arg
        if not (isinstance(node, ast.Name) and node.id in params):
            return False
    for kw in call.keywords:
        if not (isinstance(kw.value, ast.Name) and kw.value.id in params):
            return False
    return True


def _module_aliases(source: str) -> set[str]:
    """Names bound by a module-level import in the controller."""
    out: set[str] = set()
    for node in ast.parse(source).body if source else []:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                out.add(alias.asname or alias.name.split(".")[0])
    return out


def _read(fn: ast.AST, source: str, module: str) -> Optional[Forwarder]:
    """A Forwarder if `fn` is `[import]; [return] alias.attr(...)`, else None."""
    body = _significant(fn)
    imports = [n for n in body if isinstance(n, (ast.Import, ast.ImportFrom))]
    rest = [n for n in body if not isinstance(n, (ast.Import, ast.ImportFrom))]
    if len(rest) != 1:
        return None

    stmt = rest[0]
    if isinstance(stmt, ast.Return):
        returns = True
    elif isinstance(stmt, ast.Expr):
        returns = False
    else:
        return None

    call = stmt.value
    if isinstance(call, ast.Await):
        call = call.value
    if not isinstance(call, ast.Call) or not isinstance(call.func, ast.Attribute):
        return None
    if not isinstance(call.func.value, ast.Name):
        return None

    alias = call.func.value.id
    # `remote_node_controller.restart_app(engine)` forwards to the runtime it
    # was handed, not to a service module. Still a forwarder, and still worth
    # asserting -- it just gets a recording stand-in passed in rather than a
    # patched module.
    on_parameter = alias in _parameter_names(fn)
    # Every argument at the call site must come from this function's own
    # parameters. A forwarder that supplies a literal of its own --
    # `engine.restart_app([])` -- is making a decision, however small, so it
    # belongs with the behavioural tests rather than in a sweep that asserts
    # "what went in came out".
    if not _passes_only_its_own_arguments(call, fn):
        return None
    binding = next(
        (_import_source(imp, source) for imp in imports
         if any((a.asname or a.name.split(".")[0]) == alias for a in imp.names)),
        None,
    )
    if binding is None and not on_parameter and alias not in _module_aliases(source):
        return None
    return Forwarder(
        module=module,
        name=fn.name,
        alias=alias,
        attr=call.func.attr,
        is_async=isinstance(fn, ast.AsyncFunctionDef),
        returns=returns,
        local_import=binding,
        on_parameter=on_parameter,
    )


def scan() -> tuple[list[Forwarder], list[str]]:
    """(plain forwarders, qualnames of everything else).

    Private helpers (`_name`) are excluded: they are internals of the module,
    not operations the layer offers.
    """
    forwarders: list[Forwarder] = []
    complex_ones: list[str] = []
    paths = sorted(CONTROLLER_DIR.glob("*_controller.py"))
    if not paths:
        raise AssertionError(
            f"no controllers under {CONTROLLER_DIR} — the sweep would report "
            "zero forwarders and prove nothing"
        )
    for path in paths:
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if node.name.startswith("_"):
                continue
            found = _read(node, source, path.stem)
            if found is None:
                complex_ones.append(f"{path.stem}.{node.name}")
            else:
                forwarders.append(found)
    return forwarders, sorted(complex_ones)


def resolve_target(fwd: Forwarder) -> tuple[Any, str]:
    """(object to patch, attribute name) for this forwarder's service call.

    A module-level alias is read off the controller; a function-local one is
    bound by running that single import statement, which is exactly what the
    function itself does when it is called. Deferred imports are load-bearing
    in this codebase — they keep a heavy or circular import out of app boot —
    so the alternative of hoisting them to find the target would change the
    thing under test.
    """
    if fwd.on_parameter:
        raise AssertionError(
            f"{fwd.qualname} forwards to an injected object, not a module — the "
            "sweep passes it a stand-in instead of patching one"
        )
    controller = importlib.import_module(f"backend.src.controllers.{fwd.module}")
    if fwd.local_import is None:
        target = getattr(controller, fwd.alias, None)
        if target is None:
            raise AssertionError(
                f"{fwd.qualname} calls {fwd.alias}.{fwd.attr}() but nothing "
                f"binds {fwd.alias} at module level or in the function"
            )
        return target, fwd.attr

    namespace: dict[str, Any] = {}
    exec(compile(fwd.local_import, "<forwarder-import>", "exec"), namespace)  # noqa: S102
    target = namespace.get(fwd.alias)
    if target is None:
        raise AssertionError(
            f"{fwd.qualname}'s import {fwd.local_import!r} did not bind "
            f"{fwd.alias}"
        )
    return target, fwd.attr


def controller_function(fwd: Forwarder):
    controller = importlib.import_module(f"backend.src.controllers.{fwd.module}")
    return getattr(controller, fwd.name)


def build_call(fn) -> tuple[tuple, dict, list]:
    """(args, kwargs, every sentinel passed) for a call that fills the signature.

    One distinct sentinel object per parameter, so "did this exact value arrive"
    is an identity check rather than an equality one — two parameters that got
    swapped would still compare equal if they were both, say, 0.
    """
    sentinels: list[Any] = []

    def _sentinel(label: str) -> object:
        s = type("Sentinel", (), {"__repr__": lambda self, l=label: f"<{l}>"})()
        sentinels.append(s)
        return s

    args: list[Any] = []
    kwargs: dict[str, Any] = {}
    for name, param in inspect.signature(fn).parameters.items():
        if param.kind is param.VAR_POSITIONAL:
            args.append(_sentinel("vararg"))
        elif param.kind is param.VAR_KEYWORD:
            kwargs["sentinel_kwarg"] = _sentinel("varkw")
        elif param.kind is param.KEYWORD_ONLY:
            kwargs[name] = _sentinel(name)
        else:
            args.append(_sentinel(name))
    return tuple(args), kwargs, sentinels
