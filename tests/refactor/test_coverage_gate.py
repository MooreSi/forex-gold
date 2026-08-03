"""Coverage is a per-area ratchet, and the money-critical areas have a floor.

Two different jobs here, deliberately kept separate:

1. The ratchet: no area may drop below where it has already been. Runs only
   when coverage data is present, so the normal `pytest tests/` stays fast.

2. The absolute floor on money-critical areas. This one runs against the
   RECORDED BASELINE rather than live data, so it holds on every test run
   with no coverage pass needed. It is the assertion that stops somebody
   "temporarily" baselining `services/trading` down to 40% and forgetting.

Why per-area and not one global number: ~7,000 of this repo's ~34,000
statements are NiceGUI page bodies that sit near 4% and cannot be
meaningfully unit-tested without a browser. They drag the global figure to
~37% regardless of what happens to the trading logic. A single global gate is
therefore satisfiable the wrong way -- add easily-covered UI helpers, watch
the number rise, and never notice that services/trading lost a branch.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from tools.refactor_audit import coverage_gate as cg

REPO = Path(__file__).resolve().parents[2]


# The areas that decide whether real money moves correctly, and the floor
# each must never fall below. These are hand-set, not generated -- generating
# them would make them follow reality instead of constraining it.
MONEY_CRITICAL_FLOORS = {
    "backend/src/services/trading":   85.0,
    "backend/src/services/risk":      85.0,
    "backend/src/services/positions": 85.0,
    "backend/src/services/signals":   80.0,
    "backend/src/db":                 90.0,
}


def test_the_baseline_file_exists_and_parses():
    assert cg.BASELINE_PATH.exists(), (
        "no coverage baseline -- run "
        "`python -m tools.refactor_audit.coverage_gate --update-baseline`"
    )
    data = json.loads(cg.BASELINE_PATH.read_text())
    assert data["areas"], "baseline records no areas"


@pytest.mark.parametrize("area,floor", sorted(MONEY_CRITICAL_FLOORS.items()))
def test_money_critical_areas_hold_their_floor(area, floor):
    """Runs off the recorded baseline, so it holds without a coverage pass.

    If someone lowers the baseline for one of these, this fails immediately
    and names the area -- which is the whole point. The baseline is meant to
    record improvement, not to absorb regression.
    """
    baseline = json.loads(cg.BASELINE_PATH.read_text())["areas"]
    assert area in baseline, f"{area} is not being measured at all"
    assert baseline[area] >= floor, (
        f"{area} is baselined at {baseline[area]}%, below its {floor}% floor. "
        f"This area decides whether real money moves correctly. Add tests -- "
        f"do not lower the floor."
    )


def test_every_money_critical_area_is_marked_critical_in_the_tool():
    """The tool prints a marker and a louder failure for these. If the two
    lists drift apart, the marker stops meaning anything."""
    for area in MONEY_CRITICAL_FLOORS:
        assert area in cg.CRITICAL, f"{area} missing from coverage_gate.CRITICAL"


def test_no_area_has_regressed_against_its_baseline():
    """The live ratchet. Skips when no coverage data is present so the normal
    suite stays fast -- `python -m tools.checks all` produces the data."""
    if not cg.COVERAGE_JSON.exists():
        pytest.skip(
            "no .coverage.json -- run `python -m tools.checks all` to include "
            "the live coverage ratchet"
        )
    failures = cg.check()
    assert failures == [], "coverage regressed:\n  " + "\n  ".join(failures)


def test_the_area_grouping_is_stable():
    """Negative control: if area_of() changes shape, every baseline key
    silently stops matching and the whole gate goes quiet."""
    assert cg.area_of("backend/src/services/trading/open_trade.py") == \
        "backend/src/services/trading"
    assert cg.area_of("frontend/pages/trading.py") == "frontend"
    assert cg.area_of("backend/src/runtime.py") == "backend/src/runtime.py"
    assert cg.area_of("backend/src/db/database.py") == "backend/src/db"
