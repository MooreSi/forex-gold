"""No path in this app stores a licence it has not verified.

`enforce()` is fully covered by behaviour tests (`test_guard_enforce.py`,
`test_licence_recovery.py`) and they are the right instrument for what it
decides. This file pins something they cannot: that no FUTURE path writes a
licence to disk without checking its signature first.

That matters because the app's rules forbid adding a licence or auth bypass
"even for testing", and the shape such a bypass would take is not a deleted
check -- it is a NEW save site that simply never had one. A behaviour test
covers the paths that exist; this covers the ones that do not exist yet.

There are three save sites today, in two files:

    guard.py::enforce               fingerprint drift -- re-verifies against
                                    the ORIGINAL machine id before updating
    guard.py::_activate             manual activation from the licence screen
    remote/client.py MSG_LICENCE    a key pushed by the admin console

Each is guarded, in one of the two shapes a guard can take: a negative check
that returns early, or a positive check whose body contains the save.

Structural, so it costs nothing and cannot be satisfied by a mock.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

GUARDED_FILES = [
    "backend/src/config/licence/guard.py",
    "backend/src/services/cluster/remote/client.py",
]

_VERIFY_NAMES = {"verify_licence_key", "_verify_licence_key"}


def _is_licence_save(node: ast.AST) -> bool:
    """`<something ending in store>.save(...)` with a licence-shaped payload."""
    if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
        return False
    if node.func.attr != "save":
        return False
    owner = node.func.value
    name = owner.id if isinstance(owner, ast.Name) else getattr(owner, "attr", "")
    return "store" in name.lower()


def _calls_verify(node: ast.AST) -> bool:
    return any(
        isinstance(n, ast.Call)
        and (getattr(n.func, "id", None) in _VERIFY_NAMES
             or getattr(n.func, "attr", None) in _VERIFY_NAMES)
        for n in ast.walk(node)
    )


def _enclosing_functions(tree: ast.AST, target: ast.AST) -> list:
    """Every function whose body contains `target`, innermost last."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(n is target for n in ast.walk(node)):
                out.append(node)
    out.sort(key=lambda f: f.lineno)
    return out


def _guard_for(fn: ast.AST, save: ast.AST) -> str:
    """Which guard shape protects `save` inside `fn`, or "" if none does."""
    for node in ast.walk(fn):
        if not isinstance(node, ast.If):
            continue
        if not _calls_verify(node.test):
            continue

        # (a) positive: `if verify(...):` and the save is inside the body.
        if any(n is save for stmt in node.body for n in ast.walk(stmt)):
            return "positive"

        # (b) negative: `if not verify(...): return/continue`, before the save.
        exits = any(isinstance(n, (ast.Return, ast.Continue, ast.Raise))
                    for stmt in node.body for n in ast.walk(stmt))
        if exits and node.lineno < save.lineno:
            return "negative"
    return ""


def _save_sites():
    for rel in GUARDED_FILES:
        path = REPO / rel
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if _is_licence_save(node):
                yield rel, tree, node


def test_there_are_save_sites_to_check():
    """Without this, a rename of `save` would empty the parametrisation below
    and turn the whole file green and meaningless."""
    sites = list(_save_sites())

    assert len(sites) >= 3, (
        f"expected the three known licence save sites, found {len(sites)}. "
        f"If one was legitimately removed, update this number deliberately; "
        f"if `save` was renamed, _is_licence_save needs updating or this file "
        f"silently stops checking anything."
    )


def test_every_licence_save_is_guarded_by_a_signature_check():
    unguarded = []
    for rel, tree, save in _save_sites():
        fns = _enclosing_functions(tree, save)
        if not fns:
            unguarded.append(f"{rel}:{save.lineno} (at module level)")
            continue
        # Any enclosing function may hold the guard -- `_activate` is nested
        # three deep inside `_show_registration_page`.
        if not any(_guard_for(fn, save) for fn in fns):
            unguarded.append(f"{rel}:{save.lineno} in {fns[-1].name}()")

    assert unguarded == [], (
        "a licence is written to disk without its signature being checked "
        "first:\n  " + "\n  ".join(unguarded) + "\n\n"
        "Every save must sit behind either `if verify(...):` or "
        "`if not verify(...): return`. Storing an unverified key is a licence "
        "bypass, which this project forbids outright."
    )


class TestTheCheckerItselfWorks:
    """Negative controls. A checker that approved everything would make the
    gate above worthless -- and this is a gate about a security property, so
    that would be worse than not having it."""

    def _unguarded(self, source: str) -> list:
        tree = ast.parse(source)
        out = []
        for node in ast.walk(tree):
            if _is_licence_save(node):
                fns = _enclosing_functions(tree, node)
                if not fns or not any(_guard_for(fn, node) for fn in fns):
                    out.append(node.lineno)
        return out

    def test_it_catches_a_bare_save(self):
        src = "def f(k, m, e):\n    _store.save({'licence_key': k})\n"

        assert self._unguarded(src) == [2]

    def test_it_catches_a_save_guarded_by_something_else(self):
        """The realistic bypass: a check that looks like validation and is
        not."""
        src = ("def f(k, m, e):\n"
               "    if not k:\n"
               "        return\n"
               "    _store.save({'licence_key': k})\n")

        assert self._unguarded(src) == [4]

    def test_it_catches_a_verify_call_that_does_not_gate_anything(self):
        """`verify_licence_key(...)` called for its log line, with the result
        thrown away, then the save runs regardless."""
        src = ("def f(k, m, e):\n"
               "    verify_licence_key(m, e, k)\n"
               "    _store.save({'licence_key': k})\n")

        assert self._unguarded(src) == [3]

    def test_it_catches_a_verify_AFTER_the_save(self):
        src = ("def f(k, m, e):\n"
               "    _store.save({'licence_key': k})\n"
               "    if not verify_licence_key(m, e, k):\n"
               "        return\n")

        assert self._unguarded(src) == [2]

    @pytest.mark.parametrize("src,shape", [
        ("def f(k, m, e):\n"
         "    if not verify_licence_key(m, e, k):\n"
         "        return\n"
         "    _store.save({'licence_key': k})\n", "negative/return"),
        ("def f(k, m, e):\n"
         "    if verify_licence_key(m, e, k):\n"
         "        _store.save({'licence_key': k})\n", "positive"),
        ("def loop(msgs):\n"
         "    for m in msgs:\n"
         "        if not verify_licence_key(m['id'], m['exp'], m['key']):\n"
         "            continue\n"
         "        _licence_store.save(m)\n", "negative/continue"),
        ("def outer(k, m, e):\n"
         "    def inner():\n"
         "        if not _verify_licence_key(m, e, k):\n"
         "            return\n"
         "        _store_mod.save({'licence_key': k})\n"
         "    return inner\n", "nested"),
    ])
    def test_it_accepts_a_real_guard(self, src, shape):
        assert self._unguarded(src) == [], shape
