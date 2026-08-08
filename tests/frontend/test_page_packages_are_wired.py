"""A split page package resolves every name it uses.

Splitting a module into a package moves functions but not module-level
assignments. The classic result is a section that calls `log.warning(...)`
in an exception handler while `log = logging.getLogger(__name__)` stayed
behind in `__init__.py`.

That failure is invisible to every other check here. The package imports
fine, the app boots fine, the page renders fine — and then the first time
an ORB chart fails to draw, the error handler raises NameError instead of
logging, and the real error is lost. It happened during the trading split
and this test is why it was caught before the commit.

So: for every module in a split page package, resolve every global name it
loads against what that module actually has. Static, so it covers branches
no test executes — which is the point, because those are the branches this
bug hides in.
"""
from __future__ import annotations

import ast
import builtins
from pathlib import Path

import pytest

PAGES = Path(__file__).resolve().parents[2] / "frontend" / "pages"

# Names Python provides implicitly at module scope.
_MODULE_DUNDERS = {
    "__name__", "__file__", "__doc__", "__all__", "__package__",
    "__spec__", "__loader__", "__builtins__", "__path__",
}


def _page_packages() -> list[Path]:
    return sorted(p for p in PAGES.iterdir() if p.is_dir()
                  and (p / "__init__.py").exists() and p.name != "__pycache__")


def _package_modules() -> list[Path]:
    return [m for pkg in _page_packages() for m in sorted(pkg.glob("*.py"))]


def _bound_at_module_level(tree: ast.Module) -> set[str]:
    """Everything the module itself defines: imports, assignments, defs."""
    bound: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                for sub in ast.walk(target):
                    if isinstance(sub, ast.Name):
                        bound.add(sub.id)
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
            if isinstance(node.target, ast.Name):
                bound.add(node.target.id)
        elif isinstance(node, (ast.Try, ast.If, ast.For, ast.While, ast.With)):
            # conditional imports / assignments still bind at module scope
            for sub in ast.walk(node):
                if isinstance(sub, (ast.Import, ast.ImportFrom)):
                    for alias in sub.names:
                        bound.add(alias.asname or alias.name.split(".")[0])
                elif isinstance(sub, ast.Assign):
                    for target in sub.targets:
                        for name in ast.walk(target):
                            if isinstance(name, ast.Name):
                                bound.add(name.id)
    return bound


def _locally_bound(fn: ast.AST) -> set[str]:
    """Names bound inside a function: params, assignments, comprehensions,
    with/except/for targets, and nested defs."""
    bound: set[str] = set()
    args = getattr(fn, "args", None)
    if args is not None:
        for group in (args.posonlyargs, args.args, args.kwonlyargs):
            bound.update(a.arg for a in group)
        if args.vararg:
            bound.add(args.vararg.arg)
        if args.kwarg:
            bound.add(args.kwarg.arg)
    for node in ast.walk(fn):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            bound.add(node.id)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            bound.add(node.name)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ExceptHandler) and node.name:
            bound.add(node.name)
        elif isinstance(node, ast.arg):
            bound.add(node.arg)
    return bound


def _unresolved(path: Path) -> list[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    module_scope = (_bound_at_module_level(tree)
                    | set(dir(builtins)) | _MODULE_DUNDERS)

    missing: set[str] = set()
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        local = _locally_bound(node)
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and isinstance(sub.ctx, ast.Load):
                if sub.id not in local and sub.id not in module_scope:
                    missing.add(sub.id)
    return sorted(missing)


@pytest.mark.parametrize(
    "module", _package_modules(), ids=lambda p: f"{p.parent.name}/{p.name}"
)
def test_every_name_a_page_section_uses_resolves(module):
    unresolved = _unresolved(module)
    assert unresolved == [], (
        f"{module.relative_to(PAGES.parent)} uses names it does not define or "
        f"import: {unresolved}\n"
        f"Splitting a module moves functions but not module-level assignments "
        f"like `log = logging.getLogger(__name__)`. This raises NameError at "
        f"runtime, usually inside an error handler, where it replaces the real "
        f"error with a confusing one."
    )


def test_there_is_at_least_one_page_package_to_check():
    """Negative control: the parametrisation above silently passes with zero
    cases if the discovery breaks."""
    assert _page_packages(), "no split page packages found -- discovery broken"
    assert len(_package_modules()) >= 2


def test_the_scan_can_actually_see_a_missing_name(tmp_path):
    """Negative control on the detector itself, using the exact bug this
    file exists to catch."""
    broken = tmp_path / "_section.py"
    broken.write_text(
        "def render():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as e:\n"
        "        log.warning('boom %s', e)\n"
    )
    assert "log" in _unresolved(broken)

    fixed = tmp_path / "_ok.py"
    fixed.write_text(
        "import logging\n"
        "log = logging.getLogger(__name__)\n"
        "def render():\n"
        "    log.warning('fine')\n"
    )
    assert _unresolved(fixed) == []
