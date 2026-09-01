"""No repo builds an UPDATE's SET clause from unchecked keys.

The values in these statements are parameterised. The **column names** are
interpolated verbatim, straight from a dict a caller supplies:

    set_clause = ", ".join(f"{k}=?" for k in updates)

Nine repos did that. Nothing exploited it — the one path that takes a dict from
outside the app filters it through an allowlist first — but the allowlist lives
in a different module from the SQL, so the query's safety depended on every
caller remembering, including ones not written yet.

They now go through `set_clause_for`, which refuses anything that is not a
plain identifier. This gate stops the raw form coming back, in this repo or a
new one, because the next person to write an update statement will copy a
neighbouring line.

`dpm/repo.py` shows the stronger version, where the columns can be enumerated:
`if column not in MILESTONE_COLUMNS: return`. That is better wherever it is
possible; `set_clause_for` is for the callers that cannot.
"""
from __future__ import annotations

import ast
import pathlib
import re

REPO = pathlib.Path(__file__).resolve().parents[2]

# `", ".join(f"{k}=?" for k in ...)` and near variants, in any repo.
_RAW = re.compile(r'join\(\s*f["\']\{[A-Za-z_][A-Za-z0-9_]*\}=\?["\']')


# Two exemptions, each with its reason. Not a filter to keep the gate quiet --
# the first is a BETTER check than the helper, and the second is the helper.
ALLOWED = {
    "backend/src/services/signals/repo.py":
        "validates against _ADJUSTABLE_LEVEL_COLS first -- a real column "
        "allowlist, which is stronger than an identifier shape check",
    "backend/src/utils/sql_identifiers.py":
        "is the helper; this is the one place the clause is built",
}


def _py_files():
    for p in sorted((REPO / "backend").rglob("*.py")):
        if "__pycache__" in p.parts:
            continue
        if str(p.relative_to(REPO)).replace("\\", "/") in ALLOWED:
            continue
        yield p


def test_no_set_clause_is_built_from_unchecked_keys():
    offenders = []
    for p in _py_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").split("\n"), 1):
            if _RAW.search(line):
                offenders.append(f"{p.relative_to(REPO)}:{i}  {line.strip()}")

    assert offenders == [], (
        "these interpolate column names into SQL straight from caller-supplied "
        "keys:\n  " + "\n  ".join(offenders)
        + "\n\nUse backend.src.utils.sql_identifiers.set_clause_for, or check "
          "the column against a known list as dpm/repo.py does."
    )


def test_every_exemption_has_a_reason_and_still_exists():
    """An exemption for a file that has moved is an exemption for nothing."""
    for path, reason in ALLOWED.items():
        assert (REPO / path).exists(), f"{path} is exempted but no longer exists"
        assert len(reason) > 30, f"{path} has no real reason recorded"


def test_the_detector_can_still_see_the_raw_form(tmp_path):
    """Negative control. This gate reads clean, which is exactly when it needs
    proving it can find something."""
    sample = 'set_clause = ", ".join(f"{k}=?" for k in updates)'

    assert _RAW.search(sample)


def test_it_does_not_flag_the_safe_helper(tmp_path):
    assert not _RAW.search("set_clause = set_clause_for(updates)")


class TestTheHelperIsActuallyReached:
    """A helper nobody calls protects nothing."""

    def test_the_settings_update_uses_it(self):
        """The one an outside peer can reach, through a settings proposal."""
        src = (REPO / "backend/src/services/risk/risk_settings_repo.py"
               ).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "update_risk_settings")
        calls = {c.func.id for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)}

        assert "set_clause_for" in calls

    def test_the_trade_update_uses_it(self):
        src = (REPO / "backend/src/services/trading/trade_repo.py"
               ).read_text(encoding="utf-8")

        assert "set_clause_for(fields)" in src
