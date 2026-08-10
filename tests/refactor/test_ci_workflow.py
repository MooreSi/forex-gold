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
