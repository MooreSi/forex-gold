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


def _unused_imports() -> list[str]:
    source = RUNTIME.read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = _imported_names(tree)
    reexported = _reexported_names()

    # Blank out the import statements themselves, so an import does not
    # count as its own usage. Everything else -- code, comments,
    # docstrings -- is fair game; a name mentioned only in prose is still
    # unused, but this errs toward keeping rather than deleting.
    lines = source.splitlines()
    import_lines = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            import_lines.update(range(node.lineno, (node.end_lineno or node.lineno) + 1))
    body = "\n".join(l for i, l in enumerate(lines, 1) if i not in import_lines)

    return sorted(
        name for name in imported
        if name not in reexported
        and not re.search(rf"(?<![\w.]){re.escape(name)}\b", body)
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

    # `_impl`-suffixed names must not be matched by their prefix, or every
    # alias would look used.
    assert re.search(r"(?<![\w.])_check_sl_impl\b", "await _check_sl_impl(x)")
    assert not re.search(r"(?<![\w.])_check_sl_impl\b", "await _check_sl_impl_v2(x)")


def test_the_reexport_scan_finds_the_known_reexports():
    """Negative control for the trap this sweep fell into once already:
    three names were deleted as unused because nothing inside runtime.py
    referenced them, and a test that imported them from runtime broke."""
    reexported = _reexported_names()
    assert "TradingRuntime" in reexported
    for name in ("_apply_fee", "_platform_fee_rate", "_tp_level_from_extreme"):
        assert name in reexported, f"{name} is imported from runtime elsewhere"
