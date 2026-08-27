"""A failing gate must name what failed.

CI run 33105642212 reported "12 failed, 3611 passed, 7 skipped, 50 errors" and
then named none of the 12. pytest lists ERRORs after FAILEDs in its short
summary, so a blind 25-line tail was entirely ERROR lines and every FAILED name
fell off the top.

That is the guardrail-that-prints-all-good failure this repo's rules exist to
prevent, just one step subtler: the gate did go red, it simply refused to say
why. The person reading it has a 31-minute Windows CI round trip to guess with.
"""
from __future__ import annotations

from tools.checks import _failure_report


def _pytest_like(n_failed: int, n_errors: int) -> str:
    """pytest's short summary shape: FAILED lines, then ERROR lines, then the
    counts. The ordering is the whole reason a tail loses the failures."""
    lines = ["=== short test summary info ==="]
    lines += [f"FAILED tests/test_thing.py::test_case_{i} - AssertionError"
              for i in range(n_failed)]
    lines += [f"ERROR tests/test_other.py::test_err_{i} - PermissionError"
              for i in range(n_errors)]
    lines.append(f"{n_failed} failed, 3611 passed, {n_errors} errors in 1728.96s")
    return "\n".join(lines)


def test_failures_are_named_even_when_errors_would_flood_the_tail():
    """The exact CI case: 12 failures buried under 50 errors."""
    report = "\n".join(_failure_report(_pytest_like(12, 50)))
    for i in range(12):
        assert f"test_case_{i}" in report, f"failure {i} was not reported"


def test_the_counts_line_survives():
    """It is the one line that says how bad it is, and it is last -- so the
    tail still has to be shown, not just the named lines."""
    report = "\n".join(_failure_report(_pytest_like(12, 50)))
    assert "12 failed, 3611 passed" in report


def test_a_flood_is_capped_but_says_how_many_it_dropped():
    """Naming 500 failures helps nobody, but silently showing 40 of them is
    how you conclude the problem is smaller than it is."""
    report = "\n".join(_failure_report(_pytest_like(300, 0)))
    assert "and 260 more" in report


def test_output_with_no_summary_lines_still_shows_the_tail():
    """A check that dies before pytest starts -- an import error, a missing
    module -- has no FAILED lines at all. It must not report nothing."""
    output = "\n".join(f"line {i}" for i in range(60))
    report = _failure_report(output)
    assert report, "a failure with no FAILED lines reported nothing at all"
    assert "line 59" in "\n".join(report)


def test_named_lines_are_not_repeated_in_the_tail():
    """Cosmetic, but a report that prints the same 20 lines twice trains people
    to skim it."""
    report = _failure_report(_pytest_like(3, 2))
    assert report.count("FAILED tests/test_thing.py::test_case_0 - AssertionError") == 1
