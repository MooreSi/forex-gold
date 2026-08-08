"""Coverage as a per-area ratchet (not one global number).

    python -m tools.refactor_audit.coverage_gate --check
    python -m tools.refactor_audit.coverage_gate --update-baseline
    python -m tools.refactor_audit.coverage_gate --report

A single repo-wide percentage is close to useless here. This codebase is
~34,000 statements, of which ~7,000 are NiceGUI page bodies that cannot be
meaningfully unit-tested without a browser. Those pages sit near 4%, and they
drag the global figure to ~37% no matter what happens to the trading logic.

That matters because a global gate is satisfiable the wrong way: someone adds
a hundred lines of easily-covered UI helper, the global number rises, and
nobody notices that `services/trading` just lost a branch. The number went the
right way while the thing that matters got worse.

So each area carries its own floor, and each floor only rises. The areas that
decide whether real money moves -- trading, risk, positions, signals, db --
carry high floors and are treated as non-negotiable. The frontend carries a
low floor and is covered a different way: tests/frontend/ asserts every page
imports and the app boots, which catches the failure mode pages actually have
(a moved import), rather than pretending a render test exists.

Run coverage first (tools/checks.py does this for you):

    pytest tests/ -q --cov=backend --cov=frontend --cov-report=json:.coverage.json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).parent / "coverage_baseline.json"
COVERAGE_JSON = REPO_ROOT / ".coverage.json"

# Tolerance in percentage points. Coverage moves a little with test ordering
# and branch timing; a gate that trips on 0.01% gets disabled within a week.
SLACK = 0.5

# Areas whose coverage decides whether real money moves the right way. These
# are called out so a drop reads as what it is, rather than as one row in a
# table.
CRITICAL = {
    "backend/src/services/trading",
    "backend/src/services/risk",
    "backend/src/services/positions",
    "backend/src/services/signals",
    "backend/src/services/broker",
    "backend/src/db",
    "backend/src/runtime.py",
}


def area_of(path: str) -> str:
    """Group a file into the area its floor is tracked against.

    The separator is normalised first. coverage.py writes native paths, so on
    Windows every key in coverage.json is backslash-separated while the areas
    above (and the baseline file) are written with forward slashes. Without
    this, nothing matched any area, every recorded floor reported "no longer
    measured (deleted? renamed?)", and the ratchet was inert -- the same
    silent-guardrail failure mode as the import-contract scanner that could
    not decode a file and reported it clean.
    """
    path = path.replace("\\", "/")
    parts = path.split("/")
    if path.startswith("backend/src/services/"):
        # A nested service subpackage carries its own floor. Without this,
        # relocating a large untested package INTO a well-covered service
        # drags the parent's percentage down and reads as a regression, when
        # no previously-covered line lost coverage at all -- only the
        # denominator changed. services/cluster is the live case: its three
        # original modules still measure exactly their recorded 50.4%, while
        # the 2,692 statements of remote/ and sync/ that moved in from
        # controllers/ were untested before the move and still are.
        #
        # Splitting the area keeps the original floor intact AND puts an
        # honest, shrink-only floor on the moved code, which is strictly
        # better than lowering one number to cover both.
        if len(parts) > 5:
            return "/".join(parts[:5])
        if len(parts) > 3:
            return "/".join(parts[:4])
    if path.startswith("frontend/"):
        return "frontend"
    if path.endswith("runtime.py") or path.endswith("app.py"):
        return path
    if len(parts) >= 3:
        return "/".join(parts[:3])
    return path


def measure(coverage_json: Path = COVERAGE_JSON) -> dict[str, dict]:
    if not coverage_json.exists():
        raise SystemExit(
            f"no coverage data at {coverage_json}\n"
            f"run: pytest tests/ -q --cov=backend --cov=frontend "
            f"--cov-report=json:{coverage_json.name}"
        )
    data = json.loads(coverage_json.read_text(encoding="utf-8"))
    areas: dict[str, dict] = {}
    for path, entry in data["files"].items():
        area = area_of(path)
        bucket = areas.setdefault(area, {"covered": 0, "statements": 0})
        bucket["covered"] += entry["summary"]["covered_lines"]
        bucket["statements"] += entry["summary"]["num_statements"]
    for bucket in areas.values():
        total = bucket["statements"]
        bucket["percent"] = round(bucket["covered"] / total * 100, 1) if total else 100.0
    return dict(sorted(areas.items()))


def _baseline() -> dict[str, float]:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8")).get("areas", {})


def check() -> list[str]:
    """Return a list of regressions; empty means pass."""
    baseline = _baseline()
    current = measure()
    failures = []
    for area, floor in baseline.items():
        if area not in current:
            failures.append(f"{area}: no longer measured (deleted? renamed?)")
            continue
        now = current[area]["percent"]
        if now < floor - SLACK:
            mark = "CRITICAL " if area in CRITICAL else ""
            failures.append(
                f"{mark}{area}: {now:.1f}% < floor {floor:.1f}% "
                f"({current[area]['statements'] - current[area]['covered']} "
                f"statements uncovered)"
            )
    return failures


def update_baseline() -> dict[str, float]:
    current = measure()
    areas = {area: data["percent"] for area, data in current.items()}
    BASELINE_PATH.write_text(json.dumps({
        "_comment": (
            "Per-area coverage floors. Shrink-only: a number here may rise, "
            "never fall. Regenerate with --update-baseline ONLY when coverage "
            "genuinely improved, and say so in the commit."
        ),
        "areas": areas,
    }, indent=2) + "\n")
    return areas


def report() -> None:
    current = measure()
    baseline = _baseline()
    print(f"{'cov%':>7} {'floor':>7}  area")
    for area, data in current.items():
        floor = baseline.get(area)
        floor_s = f"{floor:6.1f}" if floor is not None else "     -"
        mark = " *" if area in CRITICAL else "  "
        print(f"{data['percent']:6.1f}% {floor_s}{mark} {area}")
    total_c = sum(d["covered"] for d in current.values())
    total_s = sum(d["statements"] for d in current.values())
    print(f"\n  overall: {total_c}/{total_s} = {total_c / total_s * 100:.1f}%")
    print("  * = money-critical area")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--report", action="store_true")
    args = parser.parse_args()

    if args.update_baseline:
        areas = update_baseline()
        print(f"Coverage baseline written: {len(areas)} areas")
        return 0
    if args.report:
        report()
        return 0

    failures = check()
    if failures:
        print("FAIL: coverage regressed\n")
        for failure in failures:
            print(f"  {failure}")
        print(
            "\nAdd the missing tests. Do not lower the baseline to pass -- the "
            "floor is the record of what was once covered."
        )
        return 1
    print("OK: no coverage regressions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
