"""The API layer inherits the frontend's boundary rules, verbatim.

`backend/src/api/` is the new top layer. It may ask a controller a named
question and nothing else. These tests are the enforcement; the contracts in
`tools/refactor_audit/import_contracts.py` are the same rule expressed as a
gate, and `test_import_contracts_cover_the_api_layer.py` pins that they point
at this directory rather than at `frontend/`, which after the NiceGUI removal
contains no Python at all.

Every green assertion here has a negative control beside it. A scan that
cannot go red is the `delegation_checker.py` failure this repo was rebuilt
after.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

API_ROOT = Path(__file__).resolve().parents[2] / "backend" / "src" / "api"

# server.py is the composition root. It is the one module that reaches for the
# engine handle (`backend.src.app.get_engine`) so every router can receive it
# injected instead of importing it. This mirrors exactly the single exemption
# `frontend/app/__init__.py` carried under NiceGUI, and it exists for the same
# reason: something has to wire the app up, and naming that file is honest
# where a baseline of 1 is not.
COMPOSITION_ROOT = "server.py"

FORBIDDEN_PREFIXES = ("backend.src.db", "backend.src.services")


def _imports(path: Path) -> list[tuple[int, str]]:
    """(lineno, dotted module) for every import, including function-local ones.

    A deferred import is still a dependency — that is the lesson
    `import_contracts._module_names` carries, and this scan would be trivially
    evadable without it.
    """
    out: list[tuple[int, str]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom):
            if node.level or not node.module:
                continue
            out.append((node.lineno, node.module))
            for alias in node.names:
                out.append((node.lineno, f"{node.module}.{alias.name}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
    return out


def _offenders(prefixes: tuple[str, ...], *, skip_root: bool = True) -> list[str]:
    found = []
    for path in sorted(API_ROOT.rglob("*.py")):
        if skip_root and path.name == COMPOSITION_ROOT:
            continue
        for lineno, module in _imports(path):
            if any(module == p or module.startswith(p + ".") for p in prefixes):
                found.append(f"{path.relative_to(API_ROOT.parents[2])}:{lineno} imports {module}")
    return found


def test_the_api_layer_has_python_in_it_to_scan():
    """Fails closed. A scan over an empty directory reports zero offenders and
    protects nothing, which is the precise shape of the guardrail that printed
    'all good' for months."""
    modules = [p for p in API_ROOT.rglob("*.py") if p.name != "__init__.py"]
    assert len(modules) >= 5, f"only {len(modules)} modules under {API_ROOT}"


def test_no_api_module_imports_a_service_or_the_database():
    assert _offenders(FORBIDDEN_PREFIXES) == []


def test_only_the_composition_root_reaches_for_the_engine_handle():
    """`backend.src.app` holds the live TradingRuntime. Exactly one file may
    import it; every other module receives the engine injected."""
    offenders = []
    for path in sorted(API_ROOT.rglob("*.py")):
        if path.name == COMPOSITION_ROOT:
            continue
        for lineno, module in _imports(path):
            if module == "backend.src.app" or module.startswith("backend.src.app."):
                offenders.append(f"{path.name}:{lineno}")
    assert offenders == []


def test_the_composition_root_really_does_still_need_its_exemption():
    """An exemption that outlived its reason is a pre-authorised violation in a
    file nobody watches. If server.py stops importing the app module, delete
    the exemption instead of leaving it."""
    root = API_ROOT / COMPOSITION_ROOT
    assert any(m == "backend.src.app" or m.startswith("backend.src.app.")
               for _, m in _imports(root)), (
        f"{COMPOSITION_ROOT} no longer imports backend.src.app — remove the exemption"
    )


def test_the_boundary_scan_can_see_a_violation(tmp_path):
    """Negative control. Plant each forbidden shape and prove the parser
    reports it — otherwise the two empty-list assertions above mean nothing."""
    planted = tmp_path / "planted.py"
    planted.write_text(
        "from backend.src.db import database\n"
        "import backend.src.services.trading.close_trade\n"
        "def f():\n"
        "    from backend.src.services.risk import settings\n",
        encoding="utf-8",
    )
    modules = [m for _, m in _imports(planted)]
    assert any(m.startswith("backend.src.db") for m in modules)
    assert any(m.startswith("backend.src.services") for m in modules)
    # the function-local one, which a module-level-only scan would miss
    assert "backend.src.services.risk" in modules


def test_a_controller_import_is_not_flagged(tmp_path):
    """Negative control the other way: the scan must not be so broad that the
    permitted import trips it."""
    ok = tmp_path / "ok.py"
    ok.write_text("from backend.src.controllers import trading_controller\n", encoding="utf-8")
    modules = [m for _, m in _imports(ok)]
    assert not any(m.startswith(p) for m in modules for p in FORBIDDEN_PREFIXES)
