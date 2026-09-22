"""The Python half of the ladder R:R parity contract.

The same arithmetic runs in two languages: `ladder_rr` here, and
`frontend/src/components/trading/internal/ladderRr.ts` in the EA template
editor, where the readout has to move while the operator is typing and a round
trip per keystroke would not be a readout at all.

Two implementations of one rule is the thing this repo has been burned by, so
neither side is the reference. Both are checked against
`tests/fixtures/ladder_rr_cases.json`, whose expected numbers were worked out
from the rule rather than captured from a run. The TypeScript suite reads the
same file. A change to one side that the other does not follow fails on the
side that did not move.

`tests/core/test_ladder_rr.py` is still where the behaviour is argued; this
file only pins that the shared cases are answered identically.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.src.services.broker.ea_templates import ladder_rr

CASES_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "ladder_rr_cases.json"
CASES = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]


def test_the_shared_case_file_is_actually_there():
    """A parity file the tests silently stop finding proves nothing on either side."""
    assert len(CASES) >= 7
    assert {c["id"] for c in CASES} == {
        "basic-three-level", "oversubscribed-grid", "runner-left-open",
        "close-full-banks-the-rest", "same-ladder-without-close-full",
        "zero-pip-level-is-skipped", "no-stop-no-readout",
    }


@pytest.mark.parametrize("case", CASES, ids=lambda c: c["id"])
def test_python_answers_the_shared_case(case):
    got = ladder_rr(
        case["sl_pips"],
        [tuple(level) for level in case["levels"]],
        close_full_on_last=case["close_full_on_last"],
    )

    flat = [value for row in got["rows"] for value in row]
    assert flat == pytest.approx(
        [value for row in case["rows"] for value in row]), case["why"]
    assert len(got["rows"]) == len(case["rows"]), case["why"]
    assert got["total_r"] == pytest.approx(case["total_r"]), case["why"]
    assert got["remaining"] == pytest.approx(case["remaining"])
    assert got["pct_sum"] == pytest.approx(case["pct_sum"])
