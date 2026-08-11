"""CI must run the same gate command developers run locally.

If CI stops invoking `tools.checks all` (or the file is deleted), gate
enforcement silently becomes voluntary again — the failure mode this whole
review exists to prevent. This pins the contract at the text level (parsing the
workflow YAML is awkward because `on:` is read as the boolean True).
"""
from __future__ import annotations

from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[2] / ".github" / "workflows" / "checks.yml"


def test_ci_workflow_exists():
    assert WORKFLOW.exists(), "the CI workflow is missing — gates are unenforced on push"


def test_ci_runs_the_full_checks():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python -m tools.checks all" in text, (
        "CI must run `python -m tools.checks all` — the single source of gate truth"
    )


def test_ci_triggers_on_push_and_pr():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "push:" in text and "pull_request:" in text


def test_ci_installs_the_test_dependencies():
    """requirements.txt is runtime-only — it carries no pytest. All three CI
    runs to date died in ~2m20s with 'No module named pytest' (2026-08-11
    review C3): the suite and coverage ratchet never executed, so CI was
    green-shaped noise. The workflow must install the test runner itself."""
    text = WORKFLOW.read_text(encoding="utf-8")
    for dep in ("pytest", "pytest-cov", "pytest-asyncio"):
        assert dep in text, (
            f"CI workflow does not install {dep} — the suite cannot run and "
            "every push fails before testing anything"
        )
    # Negative control: the runtime requirements really don't carry pytest —
    # if they ever do, this test's premise (and the workflow line) can simplify.
    req = (WORKFLOW.parents[2] / "requirements.txt").read_text(encoding="utf-8")
    assert "pytest" not in req
