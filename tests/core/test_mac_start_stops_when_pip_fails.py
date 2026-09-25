"""FOREX Start.command stops when the dependency install fails.

Found live 2026-09-24: a fresh download on a Mac with a full disk. pip died
with `[Errno 28] No space left on device` halfway through the requirements,
the script printed "Done.", wrote the setup-complete marker, and launched
run.py, which crashed on `No module named 'cryptography'`. Worse, the marker
held the current requirements hash, so every later launch skipped setup and
crashed the same way until the venv was deleted by hand.

`Setup & Start FOREX.bat` has always checked pip's errorlevel before writing
its marker; the Mac launcher did not.

Text assertions over the shipped script, in the manner of
tests/core/test_launchers_disarm_the_watchdog.py -- there is no way to run a
.command in the suite, and the claim worth pinning is an ORDERING inside it.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
MAC_START = REPO / "FOREX Start.command"

REQS_INSTALL = 'install --quiet --upgrade -r "$REQS_FILE"'
MARKER_WRITE = '> "$MARKER"'


def _body() -> str:
    assert MAC_START.exists(), "FOREX Start.command is a shipped launcher"
    return MAC_START.read_text(encoding="utf-8")


def test_the_requirements_install_is_a_condition_not_a_bare_command():
    """A bare pip line's exit status is thrown away by the next echo."""
    lines = [ln.strip() for ln in _body().splitlines() if REQS_INSTALL in ln]
    assert len(lines) == 1, f"expected one requirements install, found {lines}"
    assert lines[0].startswith("if ! "), (
        "the requirements pip install must be tested (`if ! ...; then`) so a "
        f"failed install cannot fall through to the launch: {lines[0]!r}")


def test_a_failed_install_exits_before_the_marker_is_written():
    """The marker is what makes the failure permanent; it must be unreachable."""
    body = _body()
    install = body.index(REQS_INSTALL)
    marker = body.index(MARKER_WRITE)
    assert install < marker, "the marker must be written after the install"
    assert "exit 1" in body[install:marker], (
        "no `exit 1` between the pip install and the marker write — a failed "
        "install still marks setup complete and launches the app")
