"""The layering contracts must point at a directory that still has code in it.

When the NiceGUI frontend was replaced by React (2026-09-18), two contracts in
`tools/refactor_audit/import_contracts.py` named `frontend` as their source
package. `frontend/` still exists — it holds the React app — so
`violations_for`'s missing-directory check would NOT have fired. It would have
walked a tree with no `.py` files in it, found zero violations, and reported
"enforced at zero" on every run for ever.

That is precisely the `delegation_checker.py` failure CLAUDE.md describes: a
guardrail scanning a deleted directory, printing "all good" for months. These
tests are the check that it cannot happen again to any contract, not just to
the two that moved.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from tools.refactor_audit.import_contracts import CONTRACTS, Contract, violations_for

REPO_ROOT = Path(__file__).resolve().parents[2]


def _python_files(package: str) -> list[Path]:
    return [p for p in (REPO_ROOT / package).rglob("*.py")]


@pytest.mark.parametrize("contract", CONTRACTS, ids=lambda c: c.name)
def test_every_contract_scans_at_least_one_python_file(contract: Contract):
    """The green-forever failure mode, closed."""
    found = sum(len(_python_files(pkg)) for pkg in contract.source_packages)
    assert found > 0, (
        f"contract {contract.name!r} scans {contract.source_packages} and finds no "
        "Python at all — it reports zero violations because there is nothing to "
        "look at, not because the boundary is held"
    )


def test_the_top_layer_contracts_name_the_api_layer():
    """The two that moved, by name, so a future refactor that quietly points
    them somewhere else has to change this test and say why."""
    by_name = {c.name: c for c in CONTRACTS}
    for name in ("the-api-layer-never-imports-the-database",
                 "the-api-layer-reaches-the-backend-through-controllers"):
        assert name in by_name, f"{name} is gone — was the top layer retired or renamed?"
        assert by_name[name].source_packages == ("backend/src/api",)
        assert by_name[name].enforced_at_zero


def test_no_contract_still_points_at_the_deleted_nicegui_frontend():
    for contract in CONTRACTS:
        assert "frontend" not in contract.source_packages, (
            f"{contract.name} still scans frontend/, which holds no Python since "
            "the React port"
        )


def test_the_missing_package_check_still_fails_closed():
    """Negative control for the parametrised test above: prove the gate's own
    hard error fires when a package is absent, rather than assuming it."""
    absent = Contract(
        name="planted", rationale="negative control",
        source_packages=("backend/src/no_such_directory",),
        forbidden=("backend.src.db",),
    )
    with pytest.raises(SystemExit) as exc:
        violations_for(absent)
    assert "cannot be enforced" in str(exc.value)


def test_the_api_layer_contract_can_still_see_a_violation(tmp_path, monkeypatch):
    """And the other half: prove the scan reports a real offender. A contract
    that is green because it cannot see anything is worse than no contract."""
    import tools.refactor_audit.import_contracts as ic

    planted = tmp_path / "backend" / "src" / "api"
    planted.mkdir(parents=True)
    (planted / "rogue.py").write_text(
        "from backend.src.db import database\n", encoding="utf-8",
    )
    monkeypatch.setattr(ic, "REPO_ROOT", tmp_path)
    contract = next(c for c in CONTRACTS
                    if c.name == "the-api-layer-never-imports-the-database")
    found = ic.violations_for(contract)
    assert [v.module for v in found] == ["backend.src.db"]
