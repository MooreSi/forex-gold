"""A launch installs dependencies only when it must, and never needs a compiler.

Found live on the VPS, 2026-11-05. The machine rebooted unexpectedly and the
app did not come back: `pip install --upgrade -r requirements.txt` failed on
`cryptg`, whose 0.6.1 release (2026-11-04) shipped a source archive only, so
pip tried to compile it and there is no Rust on the box. Version 0.6.0, with a
wheel, was already installed and working.

Two separate faults had to line up:

1. An unescaped `)` in an `echo` closed the launcher's `if DO_INSTALL` block
   early, so the install ran on EVERY launch and the requirements hash guarded
   nothing. Each restart also moved every unpinned package to its newest
   version without anyone asking.
2. Plain `--upgrade` picks the newest version even when only a source archive
   exists for it, so a new package release can stop a machine from starting.

Text assertions over the shipped scripts, in the manner of
tests/core/test_launchers_disarm_the_watchdog.py: the suite cannot run a .bat
on every platform. The parenthesis behaviour itself was verified with cmd.exe
on the VPS.
"""
from __future__ import annotations

from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
WIN_START = REPO / "Setup & Start FOREX.bat"
MAC_START = REPO / "FOREX Start.command"

BLOCK_OPEN = 'if "!DO_INSTALL!"=="1" ('
BLOCK_ELSE = ") else ("


def _early_closers(script: str) -> list[str]:
    """Lines inside the install block whose unescaped `)` would end it early."""
    start = script.index(BLOCK_OPEN) + len(BLOCK_OPEN)
    body = script[start:script.index(BLOCK_ELSE, start)]
    found = []
    for line in body.splitlines():
        stripped = line.strip()
        if not stripped.lower().startswith("echo"):
            continue
        if ")" in stripped.replace("^)", ""):
            found.append(stripped)
    return found


def test_the_scanner_finds_an_early_close():
    """Negative control: the exact line that broke the VPS must be caught."""
    bad = (
        f"{BLOCK_OPEN}\n"
        "    echo  (This only runs when requirements change or on first launch.)\n"
        f"{BLOCK_ELSE}\n"
    )
    assert _early_closers(bad) == [
        "echo  (This only runs when requirements change or on first launch.)"
    ]


def test_the_scanner_accepts_an_escaped_paren():
    good = f"{BLOCK_OPEN}\n    echo  ^(escaped^)\n{BLOCK_ELSE}\n"
    assert _early_closers(good) == []


def test_nothing_closes_the_windows_install_block_early():
    assert _early_closers(WIN_START.read_text(encoding="utf-8")) == []


def test_the_windows_launcher_prefers_wheels_before_it_installs():
    script = WIN_START.read_text(encoding="utf-8")
    assert 'set "PIP_PREFER_BINARY=1"' in script
    assert script.index('set "PIP_PREFER_BINARY=1"') < script.index(
        'pip install --quiet --upgrade -r "%REQ_FILE%"'
    )


def test_the_mac_launcher_prefers_wheels_before_it_installs():
    script = MAC_START.read_text(encoding="utf-8")
    assert "export PIP_PREFER_BINARY=1" in script
    assert script.index("export PIP_PREFER_BINARY=1") < script.index(
        'install --quiet --upgrade -r "$REQS_FILE"'
    )
