"""One command that runs everything before a commit.

    python -m tools.checks all      # the suite, every gate, the boot smoke
    python -m tools.checks gates    # just the gates (fast, no test run)
    python -m tools.checks --help

The point is that there is ONE thing to remember. A checklist spread over six
commands is a checklist with items that get skipped, and the items that get
skipped are the boring ones -- which here are the ones that catch a deleted
directory being scanned, or a page that no longer imports.

Exit code is non-zero if anything fails, so this works as a pre-commit hook or
a CI step unchanged.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import dataclass

from tools.refactor_audit.coverage_gate import COVERAGE_JSON

PY = sys.executable


@dataclass
class Check:
    name: str
    argv: list[str]
    why: str
    slow: bool = False


GATES = [
    Check(
        "structure gates",
        [PY, "-m", "tools.refactor_audit.structure_gates", "--check"],
        "file size, SQL outside the data layer, UI touching the database, "
        "undeclared multi-write transactions",
    ),
    Check(
        "import contracts",
        [PY, "-m", "tools.refactor_audit.import_contracts", "--check"],
        "the layering rules, by name",
    ),
    Check(
        "runtime facade",
        [PY, "-m", "tools.refactor_audit.facade_audit", "--check"],
        "TradingRuntime only shrinks, and its public surface is allowlisted",
    ),
    Check(
        "orphan modules",
        [PY, "-m", "tools.refactor_audit.orphan_modules", "--check"],
        "whole modules nothing imports (fails closed if an entrypoint is missing)",
    ),
    Check(
        "boot smoke",
        [PY, "-c", "import backend.src.app"],
        "the composition root still imports",
    ),
    Check(
        "doc links",
        [PY, "tools/check_doc_links.py"],
        "every relative markdown link in the living docs resolves "
        "(stage0 audit trail excluded)",
    ),
]

# The suite MUST produce coverage data, or the coverage ratchet below has
# nothing to read and fails with "no coverage data" -- which is exactly how it
# sat inert for months. --cov-report writes the JSON the gate reads; the path
# is taken from the gate itself so the two can never drift apart. (testpaths in
# pyproject is just ["tests"] since 2026-08-11 -- the frontend/tests ghost was
# removed.)
SUITE = Check(
    "test suite",
    [PY, "-m", "pytest", "tests/", "-q",
     "--cov=backend", "--cov=frontend",
     f"--cov-report=json:{COVERAGE_JSON.name}"],
    "every behaviour anyone bothered to pin",
    slow=True,
)

COVERAGE = Check(
    "coverage ratchet",
    [PY, "-m", "tools.refactor_audit.coverage_gate", "--check"],
    "coverage may rise, never fall",
)


def _clear_stale_coverage() -> None:
    """Delete any leftover coverage artifact before the suite runs.

    The coverage gate reads whatever `.coverage.json` it finds. If a previous
    run left one behind and the suite then fails to produce a new one (crash,
    interrupted run, --cov flag removed), the gate would silently grade the old
    data. Removing it first means the gate reads only what THIS run produced, or
    fails closed with "no coverage data".
    """
    try:
        COVERAGE_JSON.unlink()
    except FileNotFoundError:
        pass


def run(check: Check) -> bool:
    label = f"  {check.name}"
    print(f"{label:<24} ", end="", flush=True)
    started = time.time()
    result = subprocess.run(check.argv, capture_output=True, text=True)
    elapsed = time.time() - started
    if result.returncode == 0:
        print(f"ok   ({elapsed:.1f}s)")
        return True
    print(f"FAIL ({elapsed:.1f}s)")
    print(f"      {check.why}")
    for line in (result.stdout + result.stderr).strip().splitlines()[-25:]:
        print(f"      {line}")
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "what", nargs="?", default="all",
        choices=["all", "gates", "suite", "coverage"],
        help="all (default) runs everything; gates skips the ~5 minute suite",
    )
    args = parser.parse_args()

    if args.what == "gates":
        checks = GATES
    elif args.what == "suite":
        checks = [SUITE]
    elif args.what == "coverage":
        checks = [COVERAGE]
    else:
        checks = [*GATES, SUITE, COVERAGE]

    # If this run will both produce and grade coverage, clear any stale artifact
    # first so the gate can only ever see data from the suite that just ran.
    if SUITE in checks and COVERAGE in checks:
        _clear_stale_coverage()

    print(f"\nRunning {len(checks)} check(s)\n")
    failed = [c.name for c in checks if not run(c)]

    print()
    if failed:
        print(f"FAILED: {', '.join(failed)}")
        print(
            "\nA failing gate is not noise. If a baseline genuinely had to move,\n"
            "say why in the commit message, then re-run with --update-baseline."
        )
        return 1
    print("All checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
