"""Named layering contracts for the backend/frontend split (M5).

M1-M4 enforced structure with anonymous counters. A counter tells you a
number moved; a contract tells you which principle broke and why it
mattered. This turns the five rules the refactor plan named into checks
that report themselves by name.

Two of them the refactor has already won outright, so they are enforced at
zero -- no baseline, no allowance, any violation fails. The other three
still have real violations, so they carry a recorded count that may only
shrink. That split is deliberate. A contract suite that goes red on the
day it lands gets switched off the week it lands, and a contract that is
green only because it was written to be green is worse than no contract:
it certifies a boundary nobody is holding.

Usage:
    python -m tools.refactor_audit.import_contracts --check
    python -m tools.refactor_audit.import_contracts --update-baseline
"""
from __future__ import annotations

import argparse
import ast
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
BASELINE_PATH = Path(__file__).parent / "import_contracts_baseline.json"


@dataclass(frozen=True)
class Violation:
    path: str
    lineno: int
    module: str

    def __str__(self) -> str:
        return f"{self.path}:{self.lineno} imports {self.module}"


@dataclass(frozen=True)
class Contract:
    name: str
    rationale: str
    source_packages: tuple[str, ...]
    forbidden: tuple[str, ...]
    # Import prefixes that are allowed despite matching `forbidden`.
    allowed: tuple[str, ...] = ()
    # True  -> any violation fails (ground already taken).
    # False -> counted against a baseline that may only shrink.
    enforced_at_zero: bool = False


CONTRACTS: list[Contract] = [
    Contract(
        name="controllers-never-import-repos",
        rationale=(
            "A controller's job is to translate between the UI's shapes and a "
            "service's API. The moment it reaches past the service into that "
            "service's repo, the service stops owning its own data access and "
            "the repo's invariants (transaction boundaries, cache "
            "invalidation) are enforceable only by convention."
        ),
        source_packages=("backend/src/controllers",),
        forbidden=("repo",),
        enforced_at_zero=True,
    ),
    Contract(
        name="frontend-never-imports-the-database",
        rationale=(
            "Won in M3, and the reason the ui_db counter exists. A page that "
            "opens its own connection runs SQL on the UI event loop, which is "
            "what produced the 400-600ms stalls, and it bypasses every cache "
            "invalidation the services perform on write."
        ),
        source_packages=("frontend",),
        forbidden=("backend.src.db",),
        enforced_at_zero=True,
    ),
    Contract(
        name="frontend-reaches-the-backend-through-controllers",
        rationale=(
            "The database boundary is closed; the service boundary is not. "
            "Every page that imports a service directly is a page that can "
            "call a service function on the UI thread, and is one more caller "
            "to rewire whenever a service's signature changes. Shrinks as "
            "pages are drained; see FINISH_LINE.md."
        ),
        source_packages=("frontend",),
        forbidden=("backend.src",),
        allowed=("backend.src.controllers",),
    ),
    Contract(
        name="no-nicegui-in-the-backend",
        rationale=(
            "The backend must be runnable, testable and schedulable without a "
            "UI framework present. Both current violations are function-local "
            "imports for genuinely cross-cutting actions (the licence dialog "
            "and app shutdown), which is why they are baselined rather than "
            "banned outright -- but a module-level nicegui import in a service "
            "would make the whole backend unimportable headless."
        ),
        source_packages=("backend",),
        forbidden=("nicegui",),
    ),
    Contract(
        name="utils-and-config-depend-on-nothing-above-them",
        rationale=(
            "utils/ and config/ sit at the bottom of the stack -- everything "
            "imports them, so anything they import becomes a de facto global "
            "dependency and a cycle risk. self_healer importing the runtime is "
            "the clearest case: the lowest layer reaching for the highest."
        ),
        source_packages=("backend/src/utils", "backend/src/config"),
        forbidden=("backend.src",),
        allowed=("backend.src.utils", "backend.src.config"),
    ),
]


def _module_names(path: Path) -> list[tuple[int, str]]:
    """(lineno, dotted module) for every import in `path`, including
    function-local ones -- a deferred import is still a dependency."""
    try:
        tree = ast.parse(path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:          # relative import, not cross-package
                continue
            if node.module:
                out.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
    return out


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if module == prefix or module.startswith(prefix + "."):
            return True
        # bare-name rules like "repo" match the final segment, so that
        # `from x.y import repo` and `import x.y.repo` both count.
        if "." not in prefix and prefix in module.split("."):
            return True
    return False


def violations_for(contract: Contract) -> list[Violation]:
    found: list[Violation] = []
    for package in contract.source_packages:
        base = REPO_ROOT / package
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = path.relative_to(REPO_ROOT)
            for lineno, module in _module_names(path):
                if not _matches(module, contract.forbidden):
                    continue
                if contract.allowed and _matches(module, contract.allowed):
                    continue
                found.append(Violation(str(rel), lineno, module))
    return found


@dataclass
class Report:
    counts: dict[str, int] = field(default_factory=dict)
    regressions: list[str] = field(default_factory=list)
    details: dict[str, list[Violation]] = field(default_factory=dict)

    def render(self) -> str:
        lines = []
        for contract in CONTRACTS:
            count = self.counts[contract.name]
            if contract.enforced_at_zero:
                status = "enforced at zero" if count == 0 else f"VIOLATED ({count})"
            else:
                status = f"{count} violation(s), baselined"
            lines.append(f"  {contract.name}: {status}")
        return "\n".join(lines)


def _baseline() -> dict[str, int]:
    if not BASELINE_PATH.exists():
        return {}
    return json.loads(BASELINE_PATH.read_text())


def check() -> Report:
    baseline = _baseline()
    report = Report()
    for contract in CONTRACTS:
        found = violations_for(contract)
        report.counts[contract.name] = len(found)
        report.details[contract.name] = found
        if contract.enforced_at_zero:
            if found:
                report.regressions.append(
                    f"{contract.name}: enforced at zero but found {len(found)} "
                    f"-- e.g. {found[0]}"
                )
            continue
        allowed = baseline.get(contract.name)
        if allowed is None:
            report.regressions.append(f"{contract.name}: no baseline recorded")
        elif len(found) > allowed:
            report.regressions.append(
                f"{contract.name}: {len(found)} > baseline {allowed} "
                f"-- e.g. {found[0]}"
            )
    return report


def update_baseline() -> dict[str, int]:
    new = {
        c.name: len(violations_for(c))
        for c in CONTRACTS
        if not c.enforced_at_zero
    }
    BASELINE_PATH.write_text(json.dumps(new, indent=2) + "\n")
    return new


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--update-baseline", action="store_true")
    parser.add_argument("--show", metavar="CONTRACT",
                        help="list the violations of one contract")
    args = parser.parse_args()

    if args.update_baseline:
        for name, count in update_baseline().items():
            print(f"  {name}: {count}")
        print(f"Baseline written: {BASELINE_PATH.name}")
        return 0

    if args.show:
        contract = next((c for c in CONTRACTS if c.name == args.show), None)
        if contract is None:
            print(f"unknown contract: {args.show}", file=sys.stderr)
            return 2
        print(contract.rationale)
        for violation in violations_for(contract):
            print(f"  {violation}")
        return 0

    report = check()
    print(report.render())
    if report.regressions:
        print("\nFAIL: import contracts regressed")
        for regression in report.regressions:
            print(f"  {regression}")
        return 1
    print("\nOK: no import-contract regressions.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
