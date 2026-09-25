"""What the Windows installer does, and does not do, for a VPS.

Found on a fresh VPS install, 2026-09-25:

* The sync server listens on TCP 8765 (Settings > Remote node). The installer
  opened it for a few hours on 2026-09-25; the owner then reversed that,
  because most Windows installs are someone's main PC. The port is opened by
  "Make this node a VPS" (tests/core/test_vps_firewall.py), and only removed
  here, on uninstall.
* The first start skipped the browser, because a VPS never opens one. The
  installer now leaves the marker run.py honours once (see
  tests/test_browser_after_install.py).
"""
from __future__ import annotations

import re
from pathlib import Path

import pytest

from backend.src.services.cluster.sync.tls_util import DEFAULT_SYNC_PORT

REPO = Path(__file__).resolve().parents[2]
ISS = (REPO / "installer" / "FOREX_Trader_Setup.iss").read_text(encoding="utf-8")


def _section(name: str) -> str:
    match = re.search(rf"^\[{name}\]\s*$(.*?)(?=^\[\w+\]\s*$|\Z)", ISS, re.M | re.S)
    assert match, f"no [{name}] section"
    return match.group(1)


def _entries(section: str) -> list[str]:
    """[Run]-style entries joined across their `\\` line continuations."""
    return [e for e in re.sub(r"\\\s*\n\s*", " ", _section(section)).splitlines()
            if e.strip() and not e.strip().startswith(";")]


def test_the_installer_does_not_open_the_sync_port():
    """Owner, 2026-09-25: most Windows installs are a main PC, not a VPS, and
    must accept nothing inbound. "Make this node a VPS" opens it instead."""
    assert not any("add rule" in e and f"localport={DEFAULT_SYNC_PORT}" in e
                   for e in _entries("Run"))


def test_the_uninstaller_removes_the_rule_the_app_may_have_made():
    from backend.src.services.cluster.sync.reachability import RULE_NAME
    name = RULE_NAME.format(port=DEFAULT_SYNC_PORT)
    assert any(f'name=""{name}""' in e and "delete rule" in e
               for e in _entries("UninstallRun"))


def test_the_installer_leaves_the_marker_run_py_reads():
    run_py = (REPO / "run.py").read_text(encoding="utf-8")
    marker = re.search(r'parent / "([^"]+)"', run_py[run_py.index("_should_open_browser("
                                                               "\n"):]).group(1)
    assert f"{{app}}\\{marker}" in _section("Code")


# ── Shipping only what a commit has (2026-09-25) ─────────────────────────────
# The v6.11 install stayed "Not linked": the .exe is built from a working tree,
# and the installer copied `frontend/tsconfig.tsbuildinfo`, a build cache git
# ignores. No commit has it, so no commit matched the install.

def _files() -> list[str]:
    return [l for l in ISS.splitlines() if l.strip().startswith("Source:")]


def test_the_installer_ships_the_repos_gitignore():
    """So linking on the target ignores exactly what the repo ignores, rather
    than a hand-kept copy of the list inside the app."""
    assert any('Source: "..\\.gitignore"' in l for l in _files())


@pytest.mark.parametrize("tree", ["backend", "frontend"])
@pytest.mark.parametrize("junk", ["*.tsbuildinfo", ".DS_Store"])
def test_build_caches_are_not_copied(tree, junk):
    line = next(l for l in _files() if f'"..\\{tree}\\*"' in l)
    excludes = re.search(r'Excludes:\s*"([^"]*)"', line).group(1).split(",")
    assert junk in excludes
