"""The coverage ratchet is only real if the suite actually feeds it.

For months `tools.checks all` ran pytest WITHOUT --cov, so `.coverage.json` was
never produced and the coverage gate could not pass as documented (testing
review 2026-08-08, finding H2). Worse, had a stale `.coverage.json` ever been
left behind, the gate would have trusted it forever.

These pin two properties:
  1. the suite command produces coverage data at the path the gate reads;
  2. a stale artifact is cleared before the suite runs, so the gate only ever
     sees data from the run that just happened (or fails closed with "no
     coverage data").
"""
from __future__ import annotations

from tools import checks
from tools.refactor_audit.coverage_gate import COVERAGE_JSON


def test_suite_command_enables_coverage():
    argv = checks.SUITE.argv
    joined = " ".join(argv)
    assert "--cov=backend" in argv
    assert "--cov=frontend" in argv
    # the report must land where the gate reads it
    assert f"--cov-report=json:{COVERAGE_JSON.name}" in joined or \
           any(a.startswith("--cov-report=json:") and COVERAGE_JSON.name in a for a in argv)


def test_stale_coverage_is_cleared(tmp_path, monkeypatch):
    """A leftover artifact must not survive into the next run."""
    fake = tmp_path / ".coverage.json"
    fake.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(checks, "COVERAGE_JSON", fake)

    assert fake.exists()  # negative control: it's really there first
    checks._clear_stale_coverage()
    assert not fake.exists()


def test_clear_stale_coverage_is_safe_when_absent(tmp_path, monkeypatch):
    missing = tmp_path / ".coverage.json"
    monkeypatch.setattr(checks, "COVERAGE_JSON", missing)
    checks._clear_stale_coverage()  # must not raise
    assert not missing.exists()
