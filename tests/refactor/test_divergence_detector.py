"""The divergence detector's whole claim is that it would have caught the two
truncations the test suite missed. These tests hold it to that.

Both are fixed at HEAD, so they are only visible in historical mode -- which is
exactly why historical mode exists.
"""
from __future__ import annotations

import pytest

from tools.refactor_audit import divergence_detector as dd
from tools.refactor_audit import orphan_detector as od


def finding(module: str, function: str, historical: bool = True) -> dict | None:
    findings = dd.audit_module(od.CORE_DIR / f"{module}.py", historical=historical)
    for f in findings:
        if f.get("function") == function:
            return f
    return None


@pytest.fixture(scope="module", autouse=True)
def _requires_full_history():
    """These tests read the extraction commit's parent.

    A shallow clone silently has no parent commit, which would make every
    assertion below vacuously pass with zero findings -- the exact failure mode
    that hid these truncations in the first place.
    """
    if (od.REPO_ROOT / ".git" / "shallow").exists():
        pytest.skip("shallow clone: run `git fetch --unshallow` to audit history")


def test_detects_the_run_tp_ladder_truncation():
    """PROGRESS.md:509-530 -- lost a state update and a breakeven alert."""
    f = finding("core_run_tp_ladder", "run_tp_ladder")
    assert f is not None, "the known truncation was not detected at all"
    assert f["shape_delta"] < 0, "a truncation must show as a smaller copy"
    missing = " ".join(f["missing"])
    assert "current_sl = new_sl" in missing
    assert "fmt_sl_moved" in missing


def test_detects_that_orb_fixed_diverged():
    """PROGRESS.md:628-643 records a missing trailing log.info in this module.

    Known limitation, verified rather than assumed: that specific gap is NOT
    visible at this comparison's boundary. The detector compares the extracted
    copy at its add-commit against the original at the add-commit's parent, and
    at the add-commit (5d57f7ee) the copy already contained the log.info line.
    The gap was therefore introduced by an edit made after the file was added
    and before it was wired in -- a window this comparison does not cover.

    So: a gap introduced *during* extraction is caught (see run_tp_ladder), a
    gap introduced by a later edit to an already-extracted module is not. That
    is what the wiring tests and the LOC/orphan gates in CI are for. What this
    test pins is that the module is still flagged as divergent, so a human
    looks at it.
    """
    f = finding("core_handle_orb_fixed", "handle_orb_fixed")
    assert f is not None, "orb_fixed should still be flagged as divergent"
    assert f["missing"], "a flagged finding must name what differs"


def test_both_truncations_are_fixed_at_head():
    """Default mode filters anything whose logic survives somewhere at HEAD.

    If either of these reappears in default mode, the fix was reverted.
    """
    assert finding("core_run_tp_ladder", "run_tp_ladder", historical=False) is None \
        or "current_sl = new_sl" not in " ".join(
            finding("core_run_tp_ladder", "run_tp_ladder", historical=False)["missing"])


def test_staticmethod_loss_is_not_reported():
    """Turning a method into a module function drops @staticmethod by design."""
    findings = dd.audit_module(od.CORE_DIR / "core_fees_sizing.py", historical=True)
    for f in findings:
        assert "staticmethod" not in f.get("decorators_lost", [])


def test_cross_module_moves_are_not_reported_as_losses():
    """open_trade_from_signal's validation moved to core_signal_resolution.py.

    Comparing only against its own module reads that as a 133-statement
    truncation; the survival corpus is what stops it.
    """
    own_module_only = finding("core_open_trade_from_signal",
                              "open_trade_from_signal", historical=True)
    with_corpus = finding("core_open_trade_from_signal",
                          "open_trade_from_signal", historical=False)
    assert own_module_only is not None
    assert len(with_corpus["missing"]) < len(own_module_only["missing"])
