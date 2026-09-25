"""The Windows installer leaves a VPS ready to pair and shows the dashboard.

Found on a fresh VPS install, 2026-09-25:

* The sync server listens on TCP 8765 (Settings > Remote node), and nothing
  opened it. The installer added rules for 8888 and 9000 only, both limited to
  the Private network profile, and a VPS's network is almost always Public. The
  original app left this as a manual step on its About page.
* The first start skipped the browser, because a VPS never opens one. The
  installer now leaves the marker run.py honours once (see
  tests/test_browser_after_install.py).
"""
from __future__ import annotations

import re
from pathlib import Path

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


def _sync_rule() -> str:
    rules = [e for e in _entries("Run")
             if "firewall add rule" in e and f"localport={DEFAULT_SYNC_PORT}" in e]
    assert len(rules) == 1, f"expected one inbound rule for TCP {DEFAULT_SYNC_PORT}"
    return rules[0]


def test_the_sync_port_is_opened_inbound_over_tcp():
    rule = _sync_rule()
    assert "dir=in" in rule and "action=allow" in rule and "protocol=TCP" in rule


def test_the_sync_rule_is_not_limited_to_the_private_profile():
    """A VPS's network is Public. A Private-only rule opens nothing there."""
    assert "profile=private" not in _sync_rule().lower()


def test_the_sync_rule_is_removed_on_uninstall():
    name = re.search(r'name=""([^"]+)""', _sync_rule()).group(1)
    assert any(name in e and "delete rule" in e for e in _entries("UninstallRun"))


def test_the_installer_leaves_the_marker_run_py_reads():
    run_py = (REPO / "run.py").read_text(encoding="utf-8")
    marker = re.search(r'parent / "([^"]+)"', run_py[run_py.index("_should_open_browser("
                                                               "\n"):]).group(1)
    assert f"{{app}}\\{marker}" in _section("Code")
