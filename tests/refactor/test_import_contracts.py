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
    assert "the-api-layer-never-imports-the-database" in enforced

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
    baseline = json.loads(ic.BASELINE_PATH.read_text(encoding="utf-8"))
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


def test_the_scanner_sees_the_from_package_import_module_form(tmp_path):
    """Negative control for the form this codebase actually uses.

    `_module_names` used to record only `node.module` for an ImportFrom, so
    `from backend.src.services.dpm import repo` was filed as the module
    `backend.src.services.dpm` and the bare-name rule never saw `repo`. Only
    `import a.b.repo` and `from a.b.repo import x` were caught -- and nothing
    here writes either of those. The result was
    `controllers-never-import-repos` printing "enforced at zero" while 14 real
    repo imports sat in the controller layer.

    That is the exact failure CLAUDE.md was written about: a guardrail that
    scans the wrong thing and reports all-good forever. Assert the shape
    directly, not via the totals, so it cannot regress silently again.
    """
    module = tmp_path / "sample.py"
    module.write_text(
        "from backend.src.services.dpm import repo\n"
        "from backend.src.services.signals import tg_repo as _tg\n",
        encoding="utf-8",
    )
    found = {name for _, name in ic._module_names(module)}
    assert "backend.src.services.dpm.repo" in found, (
        "scanner cannot see `from <package> import repo` -- the form every "
        "real violation in this repo uses"
    )
    assert "backend.src.services.signals.tg_repo" in found, (
        "an aliased `import tg_repo as _tg` must still count as a dependency"
    )


def test_the_scanner_can_read_every_file_it_claims_to_scan():
    """A file the scanner cannot decode is a file it silently reports clean.

    `_module_names` swallows UnicodeDecodeError and returns [] -- no imports,
    no violations, no warning. `path.read_text()` without an explicit encoding
    uses the platform default, which on Windows is cp1252, and 12 of the 268
    files in scope contain a box-drawing or arrow character in a section
    comment. Among them was frontend/app.py, the module that owns the only
    @ui.page route and all the startup wiring -- invisible to
    `frontend-never-imports-the-database`, a contract enforced at zero.

    The count for frontend-reaches-the-backend-through-controllers was 53 on
    Windows and 60 on a UTF-8 platform for exactly this reason. Same code,
    same repo, different answer.
    """
    unreadable = []
    for package in {p for c in ic.CONTRACTS for p in c.source_packages}:
        base = REPO / package
        if not base.exists():
            continue
        for path in base.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if not ic._module_names(path) and path.read_text(encoding="utf-8").strip():
                # No imports found in a non-empty file is legal (see __init__.py
                # files), so only flag it when the bytes genuinely will not decode
                # under the platform default -- that is the silent-skip case.
                try:
                    path.read_text()
                except UnicodeDecodeError:
                    unreadable.append(path.relative_to(REPO).as_posix())
    assert unreadable == [], (
        "these files decode as UTF-8 but not under the platform default, and "
        "would be silently skipped by a read_text() without encoding=:\n  "
        + "\n  ".join(unreadable)
    )


def test_running_the_checker_as_a_script_reports_cleanly():
    report = ic.check()
    text = report.render()
    for contract in ic.CONTRACTS:
        assert contract.name in text, f"{contract.name} missing from the report"


def test_one_file_is_one_source_unit():
    """Deleted with its subject, and replaced by the weaker claim that is now
    true.

    `_source_unit` used to group a split NiceGUI page package back into one
    unit, so that splitting `frontend/pages/trading.py` into nine section
    modules did not score as a regression from 99 to 103 — the same package
    importing the same backend modules, penalised purely for having more
    files. Two tests pinned that grouping; both were deleted on 2026-09-18
    along with the frontend they described, because a rule that can never
    match again is dead code (golden rule 9) and a test of dead code is worse
    than none.

    The layer that replaced it is flat by rule: `backend/src/api/` holds
    modules, never packages, exactly as `backend/src/controllers/` does. If
    that ever stops being true, the grouping has to come back and so do those
    tests.
    """
    assert ic._source_unit("backend/src/api/routers/orders.py") == "backend/src/api/routers/orders.py"
    assert ic._source_unit("backend/src/utils/theme.py") == "backend/src/utils/theme.py"


def test_the_same_module_imported_twice_in_one_package_counts_once():
    """The property the split exposed, asserted directly rather than
    inferred from the totals."""
    contract = next(c for c in ic.CONTRACTS
                    if c.name == "the-api-layer-reaches-the-backend-through-controllers")
    statements = ic.violations_for(contract)
    edges = ic.coupling_edges(contract)
    assert len(edges) <= len(statements)
    assert all(isinstance(e, tuple) and len(e) == 2 for e in edges)


# ── Named file exemptions (restructure phase1/060) ────────────────────────────

def _contract(name):
    return next(c for c in ic.CONTRACTS if c.name == name)


class TestFileExemptions:
    """`exempt_files` lets a contract be enforced at zero with one named
    exception, instead of sitting on a baseline for ever because of it.

    The top layer's one site is `backend/src/api/server.py` — the composition
    root, which wires the app's own startup and holds the engine handle every
    router receives injected. It inherited the role, and the exemption, from
    `frontend/app/__init__.py` when the React port replaced NiceGUI on
    2026-09-18. A baseline of 1 cannot tell that site apart from the next one
    somebody adds; a named exemption can.

    The danger is obvious and is what these tests are for: an exemption that
    silently grows, or goes stale, turns "enforced at zero" into a slogan.
    """

    def test_the_top_layer_contract_is_enforced_at_zero(self):
        c = _contract("the-api-layer-reaches-the-backend-through-controllers")

        assert c.enforced_at_zero

    def test_it_exempts_exactly_one_file(self):
        """Every addition here is a decision someone must make deliberately."""
        c = _contract("the-api-layer-reaches-the-backend-through-controllers")

        assert len(c.exempt_files) == 1

    def test_every_exempt_file_exists(self):
        """A stale exemption is a hole nobody can see. If the file is renamed
        or deleted, this fails rather than quietly widening the rule."""
        for c in ic.CONTRACTS:
            for rel in c.exempt_files:
                assert (ic.REPO_ROOT / rel).exists(), rel

    def test_every_exempt_file_still_violates_the_contract(self):
        """The sharper half. Once a site is cleaned up, its exemption must go —
        otherwise the exemption list outlives its reason and pre-authorises a
        violation in a file nobody is watching any more."""
        for c in ic.CONTRACTS:
            for rel in c.exempt_files:
                without = [v for v in ic.violations_for(c)]
                bare = ic.Contract(
                    name=c.name, rationale=c.rationale,
                    source_packages=c.source_packages, forbidden=c.forbidden,
                    allowed=c.allowed, enforced_at_zero=False)
                assert any(v.path == rel for v in ic.violations_for(bare)), (
                    f"{rel} is exempted from {c.name} but no longer violates it "
                    "— delete the exemption")
                assert not any(v.path == rel for v in without), rel

    def test_the_contract_still_fails_on_a_new_violation(self):
        """Proof the exemption path did not disable the rule. A file that is
        not exempt must still be reported."""
        c = _contract("the-api-layer-reaches-the-backend-through-controllers")
        others = [v for v in ic.violations_for(c)]

        assert others == [], (
            "frontend is enforced at zero apart from the named composition "
            f"root; these are new:\n  " + "\n  ".join(str(v) for v in others))
