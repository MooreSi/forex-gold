"""runtime.py imports only what it still uses (M4 B9e).

Nine batches of dissolution left a large import block behind. Every method
that moved to a service took its logic but not its `X as _X_impl` alias,
so by the end of B9d, 78 of runtime.py's 164 imported names referred to
nothing in the file.

Dead imports are not merely untidy here. They are the residue that made
the previous refactor look finished when it was not: an import block full
of strategy handlers reads like a file that still dispatches strategies.
They also cost real import time and hide genuine cycles from the contract
checks in M5.

This test is general -- it re-derives the unused set from the file every
run, so it fails the next time a body moves out and its alias is left
behind.
"""
from __future__ import annotations

import ast
import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
RUNTIME = REPO / "backend" / "src" / "runtime.py"


def _imported_names(tree: ast.AST) -> dict[str, int]:
    """{bound name: lineno} for every module-level import."""
    names: dict[str, int] = {}
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or alias.name.split(".")[0]
                names[bound] = node.lineno
    return names


def _reexported_names() -> set[str]:
    """Names other modules import FROM runtime.

    An import can be unused inside runtime.py and still load-bearing: some
    callers do `from backend.src.runtime import _tp_level_from_extreme`
    rather than reaching for the service that owns it. Deleting those
    breaks the caller at import time with no warning from a usage scan --
    which is exactly what happened when this sweep first ran.
    """
    names: set[str] = set()
    pattern = re.compile(r"from\s+backend\.src\.runtime\s+import\s+([^\n(]+)")
    for root in ("backend", "frontend", "tests", "tools"):
        base = REPO / root
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            for match in pattern.finditer(path.read_text(encoding="utf-8")):
                for part in match.group(1).split(","):
                    names.add(part.strip().split(" as ")[0].strip())
    return names


def _names_in_annotation(node: ast.AST) -> set[str]:
    """Names referenced by an annotation, quoted or not.

    `reader: "TelegramReader"` is a real reference that happens to live
    inside a string, so the string is parsed rather than skipped. Anything
    that is not valid Python is not an annotation and is ignored.
    """
    names: set[str] = set()
    for sub in ast.walk(node):
        if isinstance(sub, ast.Name):
            names.add(sub.id)
        elif isinstance(sub, ast.Constant) and isinstance(sub.value, str):
            try:
                inner = ast.parse(sub.value, mode="eval")
            except SyntaxError:
                continue
            names |= {n.id for n in ast.walk(inner) if isinstance(n, ast.Name)}
    return names


def _used_names(tree: ast.AST) -> set[str]:
    """Every name the file actually references.

    This used to be a regex over the file's text, which counted a name
    mentioned in a comment or a docstring as used. That is not a
    theoretical hole: `is_gd2_message` survived the B9e sweep on the
    strength of one prose sentence in a docstring and nothing else. An
    import block full of strategy handlers reads like a file that still
    dispatches strategies whether the mention is code or prose, so prose
    must not keep an alias alive.

    `ast.Name` is the right unit: an `import x.y` binds `x`, a
    `from p import x` binds `x`, and both are referenced as either a bare
    Name or the root of an attribute chain -- `db_module.get()` parses to
    Attribute(value=Name('db_module')). Import statements themselves bind
    through `ast.alias`, not `ast.Name`, so an import can no longer count
    as its own usage and needs no blanking-out.
    """
    used: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Name):
            used.add(node.id)
        elif isinstance(node, (ast.AnnAssign, ast.arg)) and node.annotation is not None:
            used |= _names_in_annotation(node.annotation)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.returns is not None:
            used |= _names_in_annotation(node.returns)
    return used


def _unused_imports() -> list[str]:
    tree = ast.parse(RUNTIME.read_text(encoding="utf-8"))
    imported = _imported_names(tree)
    reexported = _reexported_names()
    used = _used_names(tree)

    return sorted(
        name for name in imported
        if name not in reexported and name not in used
    )


def test_runtime_imports_nothing_it_does_not_use():
    unused = _unused_imports()
    assert unused == [], (
        f"{len(unused)} unused imports in runtime.py -- residue from a body "
        f"that moved to a service without taking its alias:\n  "
        + "\n  ".join(unused)
    )


def test_the_detector_can_actually_see_an_unused_import():
    """Negative control: an empty result is meaningless if the scan is
    broken. Proves the matcher distinguishes used from unused."""
    tree = ast.parse("import os\nimport sys\nprint(os.getcwd())\n")
    assert _imported_names(tree) == {"os": 1, "sys": 2}
    assert "os" in _used_names(tree)
    assert "sys" not in _used_names(tree)

    # An import must not count as its own usage.
    assert "sys" not in _used_names(ast.parse("import sys\n"))

    # The root of an attribute chain is the bound name, so `import x.y`
    # used as `x.y.z()` is used.
    assert "os" in _used_names(ast.parse("import os.path\nos.path.join(a)\n"))

    # `_impl`-suffixed names must not be matched by their prefix, or every
    # alias would look used.
    used = _used_names(ast.parse("await _check_sl_impl_v2(x)\n"))
    assert "_check_sl_impl_v2" in used
    assert "_check_sl_impl" not in used


def test_prose_does_not_keep_an_import_alive():
    """The hole this scan was rewritten to close.

    `is_gd2_message` was imported by runtime.py and referenced nowhere in
    it but a single docstring line. Under the old regex-over-source scan
    that mention read as a usage and the name stayed. A docstring, a
    comment and a plain string are prose, not references.
    """
    tree = ast.parse(
        'import is_gd2_message\n'
        'def f():\n'
        '    """Deterministic parser (is_gd2_message/parse_gd2_signal)."""\n'
        '    # is_gd2_message used to be called here\n'
        '    return "is_gd2_message"\n'
    )
    assert _imported_names(tree) == {"is_gd2_message": 1}
    assert "is_gd2_message" not in _used_names(tree)

    # ...but a real call in the same file still counts.
    assert "is_gd2_message" in _used_names(ast.parse("is_gd2_message(text)\n"))


def test_a_quoted_annotation_still_counts_as_a_usage():
    """Deferred annotations are references, not prose -- runtime.py has
    `reader: "TelegramReader"`. Losing these would make the scan delete
    imports that a type checker needs."""
    assert "TelegramReader" in _used_names(
        ast.parse('def f(reader: "TelegramReader") -> None: ...\n')
    )
    assert "Tick" in _used_names(ast.parse('def f() -> "Optional[Tick]": ...\n'))
    assert "Tick" in _used_names(ast.parse('x: Optional["Tick"] = None\n'))


def test_the_reexport_scan_finds_the_known_reexports():
    """Negative control for the trap this sweep fell into once already:
    three names were deleted as unused because nothing inside runtime.py
    referenced them, and a test that imported them from runtime broke."""
    reexported = _reexported_names()
    assert "TradingRuntime" in reexported
    # Names chosen because they are load-bearing TODAY. _apply_fee and
    # _platform_fee_rate were here until 2026-09-01, when the History page
    # stopped reaching through runtime for them and runtime's own import of
    # them became genuinely dead -- so the control had to move to names that
    # can still fail. A control listing retired names passes for the wrong
    # reason, which is the one thing a control may not do.
    for name in ("_tp_level_from_extreme", "SimulationEngine"):
        assert name in reexported, f"{name} is imported from runtime elsewhere"
