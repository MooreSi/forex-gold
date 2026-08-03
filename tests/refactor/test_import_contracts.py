"""The layering rules are named contracts now, not counters (M5).

M1-M4 enforced structure with shrink-only counters: "SQL outside the data
layer: 0", "UI files importing the database: 0". Counters work, but they
do not say what the rule IS -- a number going up tells you something
broke without telling you which principle it broke.

M5 turns each rule into a named contract with its own baseline, so a
violation reports the contract by name and explains what it protects.

Two contracts are already clean and are enforced at zero: nothing may
regress into them. The other three still have violations, and those get a
recorded baseline that may only shrink. That is deliberate and honest --
a contract set that fails on day one gets disabled on day two, and a
green-because-aspirational contract is worse than a counter.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.refactor_audit import import_contracts as ic

REPO = Path(__file__).resolve().parents[2]


def test_every_contract_has_a_name_and_a_rationale():
    """A contract nobody can explain gets deleted the first time it fails."""
    assert ic.CONTRACTS, "no contracts defined"
    for contract in ic.CONTRACTS:
        assert contract.name, "contract missing a name"
        assert len(contract.rationale) > 40, (
            f"{contract.name}: rationale must say what the rule protects, "
            f"not restate the rule"
        )


def test_the_contracts_the_refactor_already_won_are_enforced_at_zero():
    """These two were achieved by M1-M3. They are not baselined -- any
    violation at all is a failure, so the ground already taken cannot be
    given back."""
    enforced = {c.name: c for c in ic.CONTRACTS if c.enforced_at_zero}
    assert "controllers-never-import-repos" in enforced
    assert "frontend-never-imports-the-database" in enforced

    for name, contract in enforced.items():
        violations = ic.violations_for(contract)
        assert violations == [], (
            f"contract '{name}' is enforced at zero but has "
            f"{len(violations)} violation(s):\n  "
            + "\n  ".join(str(v) for v in violations[:10])
        )


def test_no_contract_has_regressed_against_its_baseline():
    report = ic.check()
    assert report.regressions == [], (
        "import contracts regressed:\n  " + "\n  ".join(report.regressions)
    )


def test_the_baseline_file_matches_the_declared_contracts():
    """A stale baseline entry hides a contract that stopped running."""
    baseline = json.loads(ic.BASELINE_PATH.read_text())
    declared = {c.name for c in ic.CONTRACTS if not c.enforced_at_zero}
    assert set(baseline) == declared, (
        f"baseline/contract mismatch -- only in baseline: "
        f"{set(baseline) - declared}, only in contracts: {declared - set(baseline)}"
    )


def test_the_checker_can_actually_see_a_violation():
    """Negative control. A contract suite that reports zero because its
    scanner is broken is the failure mode this whole file guards against."""
    fake = ic.Contract(
        name="nothing-may-import-json",
        rationale="synthetic contract used only to prove the scanner works "
                  "against a rule the repo definitely violates",
        source_packages=("backend/src",),
        forbidden=("json",),
    )
    assert ic.violations_for(fake), "scanner found no `import json` in backend/src"


def test_running_the_checker_as_a_script_reports_cleanly():
    report = ic.check()
    text = report.render()
    for contract in ic.CONTRACTS:
        assert contract.name in text, f"{contract.name} missing from the report"


def test_contracts_are_counted_by_coupling_not_by_import_statements():
    """A file split must not move a contract's number.

    Splitting frontend/pages/trading.py into a package spread the same
    backend imports over nine section modules. Counting raw import
    statements scored that as a regression from 99 to 103 -- the same
    frontend package importing the same backend modules, penalised purely
    for having more files. Coupling did not change, so the number must not.

    The unit is therefore a distinct (source unit -> imported module) edge,
    where a split page package counts as ONE source unit.
    """
    assert ic._source_unit("frontend/pages/trading/_strategy.py") == "frontend/pages/trading"
    assert ic._source_unit("frontend/pages/trading/__init__.py") == "frontend/pages/trading"
    # An unsplit page is its own unit, and non-page paths are untouched.
    assert ic._source_unit("frontend/pages/chart.py") == "frontend/pages/chart.py"
    assert ic._source_unit("frontend/app.py") == "frontend/app.py"
    assert ic._source_unit("backend/src/utils/theme.py") == "backend/src/utils/theme.py"


def test_the_same_module_imported_twice_in_one_package_counts_once():
    """The property the split exposed, asserted directly rather than
    inferred from the totals."""
    contract = next(c for c in ic.CONTRACTS
                    if c.name == "frontend-reaches-the-backend-through-controllers")
    statements = ic.violations_for(contract)
    edges = ic.coupling_edges(contract)
    assert len(edges) <= len(statements)
    assert all(isinstance(e, tuple) and len(e) == 2 for e in edges)
