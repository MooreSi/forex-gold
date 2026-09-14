"""Every container these two panels rebuild on a timer is behind a diff check.

`docs/todo/bugs/030`. Eighty-nine per cent of this app's event-loop stalls are
the dashboard refreshing itself, and thirty per cent of them land on these two
files. Both rebuilt six containers from scratch every thirty seconds whether or
not anything had changed, and NiceGUI element creation cannot be moved off the
loop because UI objects are not thread-safe.

The mechanism is `frontend/components/render_cache.SectionCache`. This file
holds the wiring, because the mechanism being correct is worth nothing if a
container is added later without one -- which is exactly how the previous
attempt died: `test_signal/panel_data.change_signature` worked, and the only
panel that called it was deleted.

**The rule.** A `.clear()` on a container, inside a function that a timer
reaches, must sit behind a `cache.changed(...)` in that same function. The
alternatives are to add the guard or to add the function to `UNGATED` with a
reason. `UNGATED` is shrink-only: it may lose entries and must never gain one.

**What this does NOT check** is the property that makes gating safe: that each
render is a pure function of the payload the guard digests. A render that also
reads the wall clock, an engine's live cache or a setting will freeze whatever
it reads outside the payload. No scanner can see that, and pretending otherwise
would make this file the kind of guard that prints "all good" over ground it
never walked. It is checked by reading the render, and the reasoning for each
one is in the panel next to the guard.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

REPO = pathlib.Path(__file__).resolve().parents[2]

_PANELS = (
    "frontend/pages/reversal_panel",
    "frontend/pages/breakout_panel",
)

# Functions that clear a container and are NOT behind a diff check, each with
# the reason it does not need one. Shrink-only.
UNGATED = {
    # Drawn once when the user opens the card, and redrawn only when he presses
    # a button. Not on any timer, so there is nothing to skip.
    ("frontend/pages/reversal_panel/_capabilities.py", "_recommend"),
    ("frontend/pages/reversal_panel/_capabilities.py", "_apply"),
    ("frontend/pages/reversal_panel/_sections.py", "_shadow"),
}


def _innermost_owner(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    """Map every node to the nearest enclosing function.

    Needed because `ast.walk` over an outer `render()` also yields the bodies
    of the nested `_render_*` closures, which would credit the outer function
    with clears it does not make and let a real gap hide inside it.
    """
    owner: dict[ast.AST, ast.AST] = {}

    def visit(node, fn):
        for child in ast.iter_child_nodes(node):
            inner = child if isinstance(
                child, (ast.FunctionDef, ast.AsyncFunctionDef)) else fn
            owner[child] = inner
            visit(child, inner)

    visit(tree, None)
    return owner


def _clearing_functions(path: pathlib.Path) -> dict[str, set[str]]:
    """{function name: {names it clears}} for one module.

    Deliberately matches ANY `something.clear()` rather than names that look
    like containers. The first draft filtered on a `_area`/`_container`/`_row`
    suffix and silently missed `pending.clear()` in `_capabilities.py` -- a
    gate that over-catches asks for an UNGATED line with a reason, which is
    cheap; one that under-catches is the failure this repo was rebuilt after.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"))
    owner = _innermost_owner(tree)
    out: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "clear"
                and isinstance(node.func.value, ast.Name)):
            continue
        name = node.func.value.id
        fn = owner.get(node)
        if fn is None:
            continue
        out.setdefault(fn.name, set()).add(name)
    return out


def _guards(path: pathlib.Path, fn_name: str) -> bool:
    """True when `fn_name` in this module calls something's `.changed(...)`."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for fn in ast.walk(tree):
        if not (isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef))
                and fn.name == fn_name):
            continue
        for node in ast.walk(fn):
            if (isinstance(node, ast.Call)
                    and isinstance(node.func, ast.Attribute)
                    and node.func.attr == "changed"):
                return True
    return False


def _all_sites() -> list[tuple[str, str, set[str]]]:
    sites = []
    for pkg in _PANELS:
        for path in sorted((REPO / pkg).glob("*.py")):
            rel = path.relative_to(REPO).as_posix()
            for fn, containers in _clearing_functions(path).items():
                sites.append((rel, fn, containers))
    return sites


class TestTheScannerCanSee:
    """Negative controls first. A scanner that finds nothing is indis-
    tinguishable from a tree with nothing in it."""

    def test_it_finds_container_clears_at_all(self):
        assert _all_sites(), "the scan found no container rebuilds — it is broken"

    def test_it_credits_a_nested_closure_not_its_parent(self):
        """`render()` encloses every `_render_*`. If clears were credited to
        the enclosing function, one guard on `render` would satisfy all six."""
        sites = dict((fn, c) for _, fn, c in _all_sites())
        assert "render" not in sites or not (sites.get("render") or set()), (
            "a clear was credited to the outer render() — the ownership walk "
            "is wrong, and every nested container would pass on one guard")

    def test_a_function_with_no_guard_is_seen_as_unguarded(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def r():\n    log_area.clear()\n", encoding="utf-8")
        assert _clearing_functions(f) == {"r": {"log_area"}}
        assert _guards(f, "r") is False

    def test_a_function_with_a_guard_is_seen_as_guarded(self, tmp_path):
        f = tmp_path / "m.py"
        f.write_text("def r():\n    if c.changed('x', p):\n        log_area.clear()\n",
                     encoding="utf-8")
        assert _guards(f, "r") is True


class TestEveryTimedRebuildIsGated:

    def test_no_unaccounted_container_rebuild(self):
        gaps = sorted(
            f"{rel}::{fn} (clears {sorted(containers)})"
            for rel, fn, containers in _all_sites()
            if not _guards(REPO / rel, fn) and (rel, fn) not in UNGATED
        )
        assert not gaps, (
            "container(s) rebuilt on every refresh with no diff check:\n  "
            + "\n  ".join(gaps)
            + "\n\nEither guard it with SectionCache.changed(), or add it to "
              "UNGATED with the reason it is not on a timer. bugs/030.")

    def test_the_ungated_set_has_no_slack(self):
        """A shrink-only list with room in it is room to regress invisibly,
        and would leave this file naming a function that no longer clears
        anything."""
        unguarded = {(rel, fn) for rel, fn, _ in _all_sites()
                     if not _guards(REPO / rel, fn)}
        assert UNGATED <= unguarded, (
            f"stale UNGATED entries: {sorted(UNGATED - unguarded)}")


@pytest.mark.parametrize("rel,fn", sorted(UNGATED))
def test_the_ungated_functions_still_exist(rel, fn):
    """If one is renamed, the entry above silently stops covering anything."""
    assert fn in _clearing_functions(REPO / rel), f"{rel}::{fn}"
