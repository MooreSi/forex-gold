"""No test file may contain zero tests (stage2 phase3/010).

13 characterization files in tests/core were gutted during the refactor —
module docstring, fixtures, helpers, and not a single `def test_`. They
read as coverage while asserting nothing: the exact "green without
information" failure the golden rules name. Each had a populated
`*_surface.py` twin, so the stubs were deleted; this gate makes the class
structurally impossible to reintroduce.
"""
from __future__ import annotations

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"


def _test_functions(path: Path) -> int:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name.startswith("test")
    )


def _empty_test_files(root: Path) -> list[str]:
    return sorted(
        str(p.relative_to(root))
        for p in root.rglob("test_*.py")
        if _test_functions(p) == 0
    )


def test_every_test_file_has_a_test():
    """A test_*.py with no `def test_` is scaffolding wearing a test's name."""
    assert _empty_test_files(TESTS) == []


def test_detector_can_fail(tmp_path):
    """Negative control: a docstring-only test file is flagged."""
    stub = tmp_path / "test_stub.py"
    stub.write_text('"""Looks like a test file, asserts nothing."""\n', encoding="utf-8")
    populated = tmp_path / "test_real.py"
    populated.write_text("def test_x():\n    assert True\n", encoding="utf-8")
    assert _empty_test_files(tmp_path) == ["test_stub.py"]
