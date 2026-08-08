#!/usr/bin/env python3
"""Ratchets that stop the codebase drifting back while the refactor runs.

The last refactor was declared complete and then 32 feature commits landed on
top; `engine.py` regrew from 2,994 to 3,165 lines. Documentation did not
prevent that and will not prevent it next time. These gates do, by allowing
every current violation and refusing to let the count grow.

Four ratchets, each with a checked-in baseline:

  loc          no file over 800 lines; existing offenders may only shrink
  sql          no SQL outside a *repo*.py; the count may only fall
  transaction  a repo function with 2+ writes must wrap them in transaction()
  ui_db        the UI may not reach the database directly

Every gate is shrink-only by design. A hard failure on all 22 oversized files
today would just get switched off; a ratchet that can never tighten backwards
survives contact with real work.

Usage:
    python -m tools.refactor_audit.structure_gates             # report
    python -m tools.refactor_audit.structure_gates --check     # CI
    python -m tools.refactor_audit.structure_gates --update-baseline
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

from tools.refactor_audit import orphan_detector as od

BASELINE_PATH = od.REPO_ROOT / "tools" / "refactor_audit" / "structure_baseline.json"

LOC_CEILING = 800

# Controllers route; they do not decide. A file that only names operations and
# forwards each to one service does not reach 200 lines, so crossing this is
# evidence that logic has moved back up into the translation layer.
#
# Enforced at zero rather than baselined, because unlike the other gates here
# there is nothing to ratchet down from: the layer was rebuilt to satisfy it.
# The ceiling is deliberately well above the current largest (sync_controller,
# 166) -- most of that file is the rationale for why each command exists, and a
# ceiling that forces those docstrings out would trade the thing that makes the
# boundary understandable for a number.
CONTROLLER_LOC_CEILING = 200
CONTROLLER_DIR = "backend/src/controllers"

# Deliberately crude. This catches SQL embedded in application code, which is
# the thing being driven out; it is not a SQL parser and does not need to be.
SQL_PATTERN = re.compile(
    r"\b(SELECT\s+.+\s+FROM|INSERT\s+INTO|UPDATE\s+\w+\s+SET|DELETE\s+FROM|"
    r"CREATE\s+TABLE|ALTER\s+TABLE)\b",
    re.IGNORECASE | re.DOTALL,
)


def is_repo_file(path: Path) -> bool:
    name = path.name
    return "repo" in name or name == "database.py" or name.startswith("core_db_")


def loc_report() -> dict[str, int]:
    return {
        p.relative_to(od.REPO_ROOT).as_posix(): len(p.read_text(encoding="utf-8").splitlines())
        for p in od.production_files()
        if len(p.read_text(encoding="utf-8").splitlines()) > LOC_CEILING
    }


def controller_loc_report() -> dict[str, int]:
    """Controller modules over CONTROLLER_LOC_CEILING, and how long they are.

    Flat `<name>_controller.py` files are a documented exception to
    70-file-organisation.md's "package directories, not sibling files" rule,
    which exists for modules over 800 lines. This gate is what makes the
    exception safe: a controller cannot grow into a package's worth of code
    without failing the build first.
    """
    out: dict[str, int] = {}
    base = od.REPO_ROOT / CONTROLLER_DIR
    if not base.exists():
        return out
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        lines = len(path.read_text(encoding="utf-8").splitlines())
        if lines > CONTROLLER_LOC_CEILING:
            out[path.relative_to(od.REPO_ROOT).as_posix()] = lines
    return out


def controller_shape_report() -> list[str]:
    """Controller files that are not flat `<name>_controller.py` modules.

    A package directory under controllers/ is how remote/ and sync/ grew to
    4,950 lines of websocket server without anyone noticing they were not
    controllers at all. Flat modules keep the layer's whole contents legible
    from one `ls`.
    """
    offenders: list[str] = []
    base = od.REPO_ROOT / CONTROLLER_DIR
    if not base.exists():
        return offenders
    for path in sorted(base.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        rel = path.relative_to(base)
        if len(rel.parts) > 1:
            offenders.append(f"{path.relative_to(od.REPO_ROOT).as_posix()} (not a flat module)")
        elif rel.name != "__init__.py" and not rel.name.endswith("_controller.py"):
            offenders.append(f"{path.relative_to(od.REPO_ROOT).as_posix()} (not *_controller.py)")
    return offenders


def sql_report() -> dict[str, int]:
    """Files outside the data layer that contain SQL, and how many statements."""
    out: dict[str, int] = {}
    for path in od.production_files():
        if is_repo_file(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        count = sum(
            1 for node in ast.walk(tree)
            if isinstance(node, ast.Constant) and isinstance(node.value, str)
            and SQL_PATTERN.search(node.value)
        )
        if count:
            out[path.relative_to(od.REPO_ROOT).as_posix()] = count
    return out


# Statements that change rows. DDL is excluded on purpose: schema creation and
# migrate-on-write columns are not part of a business write, SQLite commits DDL
# implicitly anyway, and the ALTERs are routinely wrapped in try/except because
# they are expected to fail once the column exists.
#
# The first version of this gate counted every run()/execute() and so reported
# `test_signal_repo.log_analysis` and `breakout_signal_repo.init` alongside the
# one real defect. A gate with two false positives out of three findings gets
# ignored, which would have cost more than it caught.
ROW_MODIFYING = ("INSERT", "UPDATE", "DELETE", "REPLACE")


def _leading_sql(call: ast.Call) -> str | None:
    """The SQL verb of a run()/execute() call, when it can be read statically."""
    if not call.args:
        return None
    arg = call.args[0]
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        text = arg.value
    elif isinstance(arg, ast.JoinedStr) and arg.values and \
            isinstance(arg.values[0], ast.Constant) and \
            isinstance(arg.values[0].value, str):
        # f"ALTER TABLE {t} ADD COLUMN {c}" -- the verb is in the literal prefix.
        text = arg.values[0].value
    else:
        return None
    return text.strip().split(None, 1)[0].upper() if text.strip() else None


def _writes_in(node: ast.AST) -> int:
    """Calls that modify rows. An unreadable statement counts, conservatively."""
    total = 0
    for n in ast.walk(node):
        if not isinstance(n, ast.Call):
            continue
        is_db_call = (
            (isinstance(n.func, ast.Attribute) and n.func.attr in ("run", "execute"))
            or (isinstance(n.func, ast.Name) and n.func.id in ("run", "execute"))
        )
        if not is_db_call:
            continue
        verb = _leading_sql(n)
        if verb is None or verb in ROW_MODIFYING:
            total += 1
    return total


def _has_transaction(node: ast.AST) -> bool:
    for n in ast.walk(node):
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for item in n.items:
                if "transaction" in ast.unparse(item.context_expr):
                    return True
    return False


def transaction_report() -> dict[str, list[str]]:
    """Repo functions doing multiple writes without wrapping them."""
    out: dict[str, list[str]] = {}
    for path in od.production_files():
        if not is_repo_file(path):
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        offenders = [
            node.name for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
            and _writes_in(node) >= 2 and not _has_transaction(node)
        ]
        if offenders:
            out[path.relative_to(od.REPO_ROOT).as_posix()] = sorted(offenders)
    return out


def ui_db_report() -> dict[str, int]:
    """UI files reaching the database directly.

    This is the backend/frontend boundary. The contract is that `frontend/` may
    import controllers and nothing else; until the controllers exist, the count
    only has to fall. Ratcheting it down as each page is drained means the
    boundary becomes true incrementally, rather than the whole thing blocking on
    14 files' worth of untangling at once.
    """
    out: dict[str, int] = {}
    for path in od.production_files():
        if "frontend" not in path.parts:
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeDecodeError):
            continue
        hits = 0
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                # Both shapes count, and the second is the common one here:
                #   from backend.src.db.database import db   -> in node.module
                #   from forex_trader.core import database      -> in node.names
                if "database" in node.module or node.module == "sqlite3":
                    hits += 1
                else:
                    hits += sum(1 for a in node.names
                                if a.name in ("database", "sqlite3")
                                or a.name.startswith("core_db_"))
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name == "sqlite3" or alias.name.endswith(".database"):
                        hits += 1
        if hits:
            out[path.relative_to(od.REPO_ROOT).as_posix()] = hits
    return out


def current() -> dict:
    return {
        "loc": loc_report(),
        "sql": sql_report(),
        "transaction": {k: len(v) for k, v in transaction_report().items()},
        "ui_db": ui_db_report(),
    }


def load_baseline() -> dict:
    if not BASELINE_PATH.exists():
        return {"loc": {}, "sql": {}, "transaction": {}, "ui_db": {}}
    return json.loads(BASELINE_PATH.read_text(encoding="utf-8"))["baseline"]


def check(now: dict, baseline: dict) -> list[str]:
    """Every way the code got worse than the recorded baseline."""
    failures = []
    for gate, label in (("loc", "lines"), ("sql", "SQL statements"),
                        ("transaction", "unwrapped multi-write functions"),
                        ("ui_db", "direct DB imports")):
        for path, value in sorted(now[gate].items()):
            was = baseline.get(gate, {}).get(path)
            if was is None:
                failures.append(
                    f"[{gate}] {path}: new violation ({value} {label})")
            elif value > was:
                failures.append(
                    f"[{gate}] {path}: {label} rose {was} -> {value}")

    # The controller gates are enforced at zero -- no baseline, no allowance.
    for path, lines in sorted(controller_loc_report().items()):
        failures.append(
            f"[controller_loc] {path}: {lines} lines, ceiling is "
            f"{CONTROLLER_LOC_CEILING}. A controller this long is holding "
            f"logic that belongs in a service.")
    for offender in controller_shape_report():
        failures.append(f"[controller_shape] {offender}")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true", help="CI: exit 1 on regression")
    parser.add_argument("--update-baseline", action="store_true",
                        help="record the current state as the new ceiling")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    now = current()

    if args.update_baseline:
        BASELINE_PATH.write_text(json.dumps({
            "_comment": [
                "Shrink-only ratchet. Every number here may fall and may not rise;",
                "a file absent from a section may not appear in it.",
                "Regenerate with: python -m tools.refactor_audit.structure_gates",
                "--update-baseline, and only when the totals have gone DOWN."
            ],
            "baseline": now,
        }, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"Baseline written to {BASELINE_PATH.relative_to(od.REPO_ROOT)}")
        return 0

    if args.json:
        print(json.dumps(now, indent=2, sort_keys=True))
        return 0

    print(f"Files over {LOC_CEILING} LOC: {len(now['loc'])}"
          f"  (largest: {max(now['loc'].values(), default=0)})")
    print(f"Files with SQL outside the data layer: {len(now['sql'])}"
          f"  ({sum(now['sql'].values())} statements)")
    print(f"Repo files with unwrapped multi-write functions: {len(now['transaction'])}"
          f"  ({sum(now['transaction'].values())} functions)")
    print(f"UI files importing the database directly: {len(now['ui_db'])}"
          f"  ({sum(now['ui_db'].values())} imports)")
    _ctl_loc, _ctl_shape = controller_loc_report(), controller_shape_report()
    print(f"Controllers over {CONTROLLER_LOC_CEILING} LOC: {len(_ctl_loc)}"
          f"  (enforced at zero)")
    print(f"Controllers not flat *_controller.py modules: {len(_ctl_shape)}"
          f"  (enforced at zero)")

    if args.check:
        failures = check(now, load_baseline())
        if failures:
            print(f"\nFAIL: {len(failures)} structural regression(s):", file=sys.stderr)
            for f in failures:
                print(f"  {f}", file=sys.stderr)
            print("\nThese gates are shrink-only. If a number legitimately had to "
                  "rise, say why in the commit and rerun with --update-baseline.",
                  file=sys.stderr)
            return 1
        print("\nOK: no structural regressions against the baseline.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
