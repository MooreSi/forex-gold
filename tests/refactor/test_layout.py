"""Test-tree layout rules (stage2 phase3/030).

The hazards these pin, each found live in this repo:
- a test dir without __init__.py shares basenames with siblings, so pytest
  imports two files as one module and the collection order decides which
  actually runs (tests/reversal_engine shared 3 basenames);
- a `testpaths` entry pointing at a ghost dir reads as coverage that
  doesn't exist (frontend/tests held only an __init__.py);
- import-time os.environ/db.init()/sys.path mutation in a test module runs
  side effects the moment ANY collection touches the file.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"


def _dirs_with_tests(root: Path) -> set[Path]:
    return {p.parent for p in root.rglob("test_*.py")}


def _non_package_dirs(root: Path) -> list[str]:
    return sorted(
        str(d.relative_to(root)) or "."
        for d in _dirs_with_tests(root)
        if not (d / "__init__.py").exists()
    )


def test_all_test_dirs_are_packages():
    assert _non_package_dirs(TESTS) == []


def test_the_package_check_can_fail(tmp_path):
    """Negative control for the scan above."""
    bare = tmp_path / "bare"
    bare.mkdir()
    (bare / "test_x.py").write_text("def test_x():\n    assert True\n", encoding="utf-8")
    assert _non_package_dirs(tmp_path) == ["bare"]


def test_no_duplicate_basenames_without_packages():
    """Basenames repeat across service dirs by design; every dir holding a
    repeated basename must be a package or one file silently shadows the
    other."""
    files = list(TESTS.rglob("test_*.py"))
    dupes = {name for name, n in Counter(p.name for p in files).items() if n > 1}
    offenders = sorted(
        str(p.relative_to(TESTS))
        for p in files
        if p.name in dupes and not (p.parent / "__init__.py").exists()
    )
    assert offenders == []
    assert dupes, "sanity: the repeated-basename situation this guards still exists"


def test_testpaths_has_no_ghost_dir():
    """Every pytest testpaths entry must exist and actually contain tests."""
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")
    import re

    m = re.search(r"testpaths\s*=\s*\[([^\]]*)\]", pyproject)
    assert m, "no testpaths configured"
    entries = re.findall(r'"([^"]+)"', m.group(1))
    assert entries, "testpaths is empty"
    for entry in entries:
        target = REPO / entry
        assert target.is_dir(), f"testpaths entry {entry!r} does not exist"
        assert any(target.rglob("test_*.py")), f"testpaths entry {entry!r} holds no tests"


def _import_time_mutations(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    hits: list[str] = []
    for node in tree.body:  # module top level only — defs/classes are fine
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        for sub in ast.walk(node):
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Attribute):
                target = ast.dump(sub.func.value)
                if sub.func.attr == "init" and ("'db'" in target or "'db_module'" in target):
                    hits.append("db.init at import time")
                if sub.func.attr == "insert" and "'path'" in target:
                    hits.append("sys.path.insert at import time")
            if isinstance(sub, ast.Assign):
                for tgt in sub.targets:
                    if isinstance(tgt, ast.Subscript) and "environ" in ast.dump(tgt.value):
                        hits.append("os.environ assignment at import time")
    return sorted(set(hits))


def test_no_import_time_env_or_db_mutation():
    offenders = {
        str(p.relative_to(TESTS)): mutations
        for p in TESTS.rglob("test_*.py")
        if (mutations := _import_time_mutations(p))
    }
    assert offenders == {}


def test_the_mutation_scan_can_fail(tmp_path):
    """Negative control: each mutation species is detected."""
    p = tmp_path / "test_dirty.py"
    p.write_text(
        "import os, sys\n"
        "from pathlib import Path\n"
        "from backend.src.db import database as db_module\n"
        'os.environ["X"] = "1"\n'
        "sys.path.insert(0, str(Path(__file__).parent))\n"
        'db_module.init("x.db")\n',
        encoding="utf-8",
    )
    hits = _import_time_mutations(p)
    assert "os.environ assignment at import time" in hits
    assert "sys.path.insert at import time" in hits
    assert "db.init at import time" in hits
