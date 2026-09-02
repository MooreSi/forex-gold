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


# ---------------------------------------------------------------------------
# CI must install what `tools.checks all` actually invokes (review 2026-08-11,
# C3). All three real CI runs to date failed in ~2m20s with "No module named
# pytest": the workflow installs only requirements.txt, which declares no test
# runner — so the suite and the coverage ratchet never execute, and gate
# enforcement silently stays local-voluntary. Green here means the workflow
# provably installs the suite's tooling, wherever it declares it.

import re as _re


def _requirement_names(requirements_text: str) -> set[str]:
    """Package names from requirements-file text (comments and pins stripped)."""
    names = set()
    for line in requirements_text.splitlines():
        line = line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        names.add(_re.split(r"[=<>!~\[; ]", line, 1)[0].lower())
    return names


def _packages_pip_installs(workflow_text: str, read_requirements) -> set[str]:
    """Every package name any `pip install` line in the workflow installs,
    resolving `-r <file>` through *read_requirements(path) -> str`."""
    names: set[str] = set()
    for args in _re.findall(r"pip install (.+)", workflow_text):
        tokens = args.split()
        i = 0
        while i < len(tokens):
            if tokens[i] == "-r":
                names |= _requirement_names(read_requirements(tokens[i + 1]))
                i += 2
            elif tokens[i].startswith("-"):
                i += 1
            else:
                names.add(_re.split(r"[=<>!~\[;]", tokens[i], 1)[0].lower())
                i += 1
    return names


def _packages_ci_installs() -> set[str]:
    repo = WORKFLOW.parents[2]
    return _packages_pip_installs(
        WORKFLOW.read_text(encoding="utf-8"),
        lambda path: (repo / path).read_text(encoding="utf-8"),
    )


def test_ci_installs_pytest_so_the_suite_step_can_run():
    assert "pytest" in _packages_ci_installs(), (
        "the workflow never installs pytest — `tools.checks all` dies at the "
        "suite step with 'No module named pytest' (observed on every CI run "
        "to date) and behaviour enforcement stays local-voluntary"
    )


def test_ci_installs_pytest_cov_so_the_coverage_ratchet_has_data():
    assert "pytest-cov" in _packages_ci_installs(), (
        "tools.checks runs pytest with --cov and the ratchet reads the JSON "
        "it writes — without pytest-cov, CI can never enforce coverage"
    )


def test_the_dependency_scan_can_actually_see_a_package():
    """Negative control: resolves both inline installs and -r files."""
    found = _packages_pip_installs(
        "run: |\n  pip install --upgrade pip\n  pip install -r reqs.txt extra-pkg==1.0\n",
        {"reqs.txt": "pytest-cov==5.0  # pinned\n# comment\npyyaml>=6\n"}.__getitem__,
    )
    assert found == {"pip", "pytest-cov", "pyyaml", "extra-pkg"}


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


def test_the_coverage_artifact_can_actually_be_uploaded():
    """`.coverage.json` is a dotfile and upload-artifact v4 excludes hidden
    files by default, so the upload step ran green and produced nothing on
    every build since it was added -- "No files were found with the provided
    path" is a warning, not a failure.

    It was only noticed when the data was needed: a coverage floor failed on
    Windows only, and the artifact that would have explained it did not exist.
    A diagnostic nobody reads is the same failure as a gate nobody reads.
    """
    wf = (Path(__file__).resolve().parents[2]
          / ".github/workflows/checks.yml").read_text(encoding="utf-8")
    step = wf[wf.index("name: coverage-json"):]
    step = step[:step.index("if-no-files-found")]

    assert "include-hidden-files: true" in step, (
        "the coverage artifact is a dotfile; without include-hidden-files the "
        "upload silently uploads nothing")
