"""The module-orphan detector must find dead modules AND fail closed.

The failure it replaces sat green for months because it scanned a directory
that no longer existed (testing review H1). So the two properties that matter
most here are: (1) it actually detects a module nothing imports, and (2) it
errors loudly — never passes — when its entrypoints or roots are missing. Both
get negative controls.
"""
from __future__ import annotations

import textwrap

import pytest

from tools.refactor_audit import orphan_modules as om


def _write(root, dotted, body=""):
    p = root / (dotted.replace(".", "/") + ".py")
    p.parent.mkdir(parents=True, exist_ok=True)
    # ensure every parent is a package
    for parent in list(p.parents):
        if parent == root:
            break
        (parent / "__init__.py").touch()
    p.write_text(textwrap.dedent(body), encoding="utf-8")
    return p


@pytest.fixture
def tree(tmp_path):
    """A tiny synthetic app: entry -> used; orphan is imported by no one."""
    _write(tmp_path, "entry", "import pkg.used\n")
    _write(tmp_path, "pkg.used", "VALUE = 1\n")
    _write(tmp_path, "pkg.orphan", "DEAD = 1\n")
    return tmp_path


def test_orphan_module_is_detected(tree):
    orphans = {o["module"] for o in om.find_orphans(tree, entrypoints=("entry",))}
    assert "pkg.orphan" in orphans


def test_imported_module_is_not_flagged(tree):
    """Negative control: the reachable module must NOT be reported."""
    orphans = {o["module"] for o in om.find_orphans(tree, entrypoints=("entry",))}
    assert "pkg.used" not in orphans
    assert "entry" not in orphans


def test_wiring_in_an_orphan_clears_it(tree):
    """If the entry starts importing the orphan, it stops being one."""
    (tree / "entry.py").write_text("import pkg.used\nimport pkg.orphan\n", encoding="utf-8")
    orphans = {o["module"] for o in om.find_orphans(tree, entrypoints=("entry",))}
    assert "pkg.orphan" not in orphans


def test_package_init_is_never_reported(tree):
    _write(tree, "lonely.thing", "X = 1\n")  # creates lonely/__init__.py too
    orphans = {o["module"] for o in om.find_orphans(tree, entrypoints=("entry",))}
    # the submodule is an orphan, but no __init__ package appears in the report
    assert "lonely.thing" in orphans
    assert "lonely" not in orphans
    assert "pkg" not in orphans


def test_reached_submodule_marks_its_package_reached(tmp_path):
    _write(tmp_path, "entry", "import pkg.used\n")
    _write(tmp_path, "pkg.used", "VALUE = 1\n")
    # pkg/__init__.py exists (scaffolding) and is never reported anyway,
    # but it must be counted as reached, not dead.
    graph = om.build_graph(om.production_modules(tmp_path), tmp_path)
    reached = om.reachable(om.production_modules(tmp_path), graph, ("entry",))
    assert "pkg" in reached


def test_missing_entrypoint_fails_closed(tree):
    """The negative control for the deleted-directory failure mode."""
    with pytest.raises(SystemExit):
        om.find_orphans(tree, entrypoints=("does_not_exist",))


def test_missing_root_fails_closed(tmp_path):
    with pytest.raises(SystemExit):
        om.find_orphans(tmp_path / "nope", entrypoints=("entry",))


# ---- against the real tree -------------------------------------------------

def test_real_entrypoints_all_exist():
    module_map = om.production_modules(om.REPO_ROOT)
    for e in om.ENTRYPOINTS:
        assert e in module_map, f"entrypoint {e} not found in the tree"


def test_real_orphans_are_all_allowlisted():
    """The gate contract: every real orphan is a known, recorded debt."""
    orphans = {o["module"] for o in om.find_orphans()}
    allowed = om.load_allowlist()
    assert orphans <= allowed, f"unrecorded orphan modules: {sorted(orphans - allowed)}"


def test_allowlist_has_no_stale_entries():
    """A ledger entry that is no longer an orphan must be removed, not trusted."""
    orphans = {o["module"] for o in om.find_orphans()}
    allowed = om.load_allowlist()
    assert allowed <= orphans, f"stale allowlist entries: {sorted(allowed - orphans)}"
