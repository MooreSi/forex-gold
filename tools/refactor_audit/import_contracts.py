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
        name="controllers-never-import-the-database",
        rationale=(
            "A controller that imports db.database does not need a service to "
            "exist, so none gets written and the logic pools in the controller "
            "instead -- which is how history/controller.py ended up owning "
            "three-source ledger merges. db/database.py also re-exports ~90 "
            "names upward from services, so `db_module.get_risk_settings()` "
            "reaches a service repo without ever naming it, and the "
            "never-import-repos contract cannot see it happen."
        ),
        source_packages=("backend/src/controllers",),
        forbidden=("backend.src.db",),
        enforced_at_zero=True,
    ),
    Contract(
        name="services-never-import-controllers",
        rationale=(
            "Layers point downward. A service importing a controller is not a "
            "style problem, it is proof the module is filed in the wrong "
            "layer: 40 service call sites imported controllers.sync, which is "
            "how 2,276 lines of cluster networking came to live in the "
            "controller directory. Enforced at zero because there is no "
            "legitimate reason for behaviour to depend on translation."
        ),
        source_packages=("backend/src/services",),
        forbidden=("backend.src.controllers",),
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
    function-local ones -- a deferred import is still a dependency.

    An `ImportFrom` yields TWO names per alias: the package as written, and
    the package joined to the imported name. `from x.y import repo` cannot be
    told apart statically from `from x.y import SOME_CONSTANT`, so both forms
    are emitted and the contract's own rules decide which matters.

    Emitting only `node.module` is what let `controllers-never-import-repos`
    report "enforced at zero" for months while 14 controller files imported a
    service repo: every one of them uses `from <package> import <repo module>`,
    and the bare-name rule below only ever saw `<package>`. `violations_for`
    collapses the pair back to one violation per statement so this widening
    cannot inflate a baselined count.
    """
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    out: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.level:          # relative import, not cross-package
                continue
            if node.module:
                out.append((node.lineno, node.module))
                for alias in node.names:
                    out.append((node.lineno, f"{node.module}.{alias.name}"))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                out.append((node.lineno, alias.name))
    return out


def _matches(module: str, prefixes: tuple[str, ...]) -> bool:
    for prefix in prefixes:
        if module == prefix or module.startswith(prefix + "."):
            return True
        # Bare-name rules like "repo" match a whole path segment, so that
        # `from x.y import repo` and `import x.y.repo` both count.
        #
        # The suffix form matters as much as the exact one: this codebase
        # names most repos after their table, not after the layer --
        # trade_history_repo, tg_repo, sync_repo, ai_analysis_repo. Matching
        # only the exact segment "repo" caught 4 of the 14 real violations
        # and let the other 10 read as compliant. A segment is a repo when it
        # IS the name or ENDS WITH _<name>; "reporting" is neither, and must
        # stay clean or the rule starts flagging the analytics service.
        if "." not in prefix and any(
            segment == prefix or segment.endswith("_" + prefix)
            for segment in module.split(".")
        ):
            return True
    return False


def _source_unit(path: str) -> str:
    """The unit a violation is attributed to.

    A page split into a package is ONE source unit, not one per section.
    Counting raw import statements made the metric move when a file was
    split into a directory -- the same frontend package importing the same
    backend modules scored worse purely because the statements were spread
    over more files. Coupling did not change, so the number should not.
    """
    parts = path.split("/")
    if len(parts) > 3 and parts[0] == "frontend" and parts[1] == "pages":
        return "/".join(parts[:3])
    return path


def violations_for(contract: Contract) -> list[Violation]:
    found: list[Violation] = []
    for package in contract.source_packages:
        base = REPO_ROOT / package
        if not base.exists():
            continue
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            # as_posix, not str: _source_unit and every prefix rule below
            # split on "/", so a Windows backslash path silently matches
            # nothing and the coupling-edge dedup stops working.
            rel = path.relative_to(REPO_ROOT).as_posix()
            # One import statement is one violation. _module_names emits both
            # `x.y` and `x.y.name` for a `from x.y import name`; keeping the
            # SHORTEST name that matched preserves the module string every
            # existing baseline was counted on, while still letting the longer
            # form be the thing that trips a bare-name rule like "repo".
            per_statement: dict[int, str] = {}
            for lineno, module in _module_names(path):
                if not _matches(module, contract.forbidden):
                    continue
                if contract.allowed and _matches(module, contract.allowed):
                    continue
                current = per_statement.get(lineno)
                if current is None or len(module) < len(current):
                    per_statement[lineno] = module
            for lineno, module in sorted(per_statement.items()):
                found.append(Violation(rel, lineno, module))
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
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))


def coupling_edges(contract: Contract) -> set[tuple[str, str]]:
    """Distinct (source unit -> imported module) pairs. This is what the
    contracts are counted on -- see _source_unit."""
    return {(_source_unit(v.path), v.module) for v in violations_for(contract)}


def check() -> Report:
    baseline = _baseline()
    report = Report()
    for contract in CONTRACTS:
        found = violations_for(contract)
        report.counts[contract.name] = len(coupling_edges(contract))
        report.details[contract.name] = found
        if contract.enforced_at_zero:
            if found:
                report.regressions.append(
                    f"{contract.name}: enforced at zero but found {len(found)} "
                    f"-- e.g. {found[0]}"
                )
            continue
        measured = report.counts[contract.name]
        allowed = baseline.get(contract.name)
        if allowed is None:
            report.regressions.append(f"{contract.name}: no baseline recorded")
        elif measured > allowed:
            report.regressions.append(
                f"{contract.name}: {measured} > baseline {allowed} "
                f"-- e.g. {found[0]}"
            )
    return report


def update_baseline() -> dict[str, int]:
    new = {
        c.name: len(coupling_edges(c))
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
