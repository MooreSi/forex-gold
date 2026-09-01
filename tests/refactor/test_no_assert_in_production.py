"""Production code must not rely on `assert` for a runtime guard.

`python -O` strips assert statements entirely. Anything relying on one for a
check becomes a no-op, silently — the guard is gone and the failure surfaces
somewhere else, as a confusing error about a value that should never have got
that far.

Nothing runs this app with `-O` today. That is the point: the two that existed
(`get_engine`, `get_tg_reader` in `app.py` — the app's only guard against
handing out an uninitialised engine) would have become no-ops the first time
anyone added the flag for performance, and the symptom would have been an
AttributeError far from the cause.

Tests are exempt: `assert` is what they are for, and pytest never runs them
with -O.
"""
from __future__ import annotations

import ast
import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]
ROOTS = ("backend", "frontend", "tools")


def _asserts_in(path: pathlib.Path) -> list:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except (SyntaxError, UnicodeDecodeError):
        return []
    return [n.lineno for n in ast.walk(tree) if isinstance(n, ast.Assert)]


def test_no_assert_is_used_as_a_runtime_guard():
    offenders = []
    for root in ROOTS:
        for p in sorted((REPO / root).rglob("*.py")):
            if "__pycache__" in p.parts:
                continue
            for line in _asserts_in(p):
                offenders.append(f"{p.relative_to(REPO)}:{line}")

    assert offenders == [], (
        "these are stripped by `python -O`, so the check silently disappears:"
        "\n  " + "\n  ".join(offenders)
        + "\n\nRaise instead — RuntimeError or ValueError as fits."
    )


def test_the_detector_finds_one_when_there_is_one(tmp_path):
    """Negative control: this gate reads clean, so it needs proving."""
    f = tmp_path / "sample.py"
    f.write_text("def f(x):\n    assert x is not None\n    return x\n",
                 encoding="utf-8")

    assert _asserts_in(f) == [2]


def test_tests_themselves_are_not_scanned():
    """Asserting is what a test is. Scanning them would report thousands and
    the gate would be turned off within the hour."""
    assert "tests" not in ROOTS
