"""What Settings > Remote node tells a VPS operator about being reachable.

On a fresh VPS install (2026-09-25) the tab said "listening" on port 8765 and
nothing else: not the address the other machine should dial, not whether the
Windows firewall would let it in, not what protects the link. The original app
kept the firewall step on its About page and never showed the address at all.

Nothing here opens a socket to anywhere or changes a firewall. It reads.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

from backend.src.services.cluster.sync import reachability as reach

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def netsh(monkeypatch):
    """A Windows with a scripted `netsh`, counting how often it is asked."""
    calls = []

    def _run(args, **_kw):
        calls.append(args)
        return subprocess.CompletedProcess(args, calls_rc["rc"], "", "")

    calls_rc = {"rc": 0}
    monkeypatch.setattr(reach, "_platform", "win32")
    monkeypatch.setattr(reach, "_run", _run)
    monkeypatch.setattr(reach, "_addresses", lambda: ["38.253.124.25"])
    reach.reset_cache()
    return calls, calls_rc


class TestTheAddress:
    def test_it_names_the_address_the_other_machine_dials(self, monkeypatch):
        monkeypatch.setattr(reach, "_addresses", lambda: ["38.253.124.25"])

        out = reach.describe(8765)

        assert out["addresses"] == ["38.253.124.25"]
        assert out["behind_nat"] is False

    def test_a_private_address_only_is_flagged_as_behind_nat(self, monkeypatch):
        """Cloud VPSs often hold a 10.x address and are reached through a
        public one the machine cannot see. Showing 10.0.0.4 as the thing to
        dial would send the operator to an address that never answers."""
        monkeypatch.setattr(reach, "_addresses", lambda: ["10.0.0.4"])

        assert reach.describe(8765)["behind_nat"] is True

    def test_the_real_lookup_never_lists_loopback(self):
        assert all(not a.startswith("127.") for a in reach._addresses())


class TestTheFirewall:
    def test_the_installers_rule_is_reported_as_open(self, netsh):
        assert reach.describe(8765)["firewall"] == "open"

    def test_a_missing_rule_says_so_and_gives_the_command(self, netsh):
        _, rc = netsh
        rc["rc"] = 1

        out = reach.describe(9001)

        assert out["firewall"] == "missing"
        assert "localport=9001" in out["firewall_command"]
        assert "profile=private" not in out["firewall_command"]

    def test_it_looks_for_the_rule_the_installer_creates(self, netsh):
        """Two names for one rule is a check that always says "missing"."""
        calls, _ = netsh
        reach.describe(8765)

        iss = (REPO / "installer" / "FOREX_Trader_Setup.iss").read_text(encoding="utf-8")
        installer_names = re.findall(r'add rule name=""([^"]+)""', iss)
        assert any(f"name={n}" in calls[0] for n in installer_names)

    def test_netsh_is_asked_once_a_minute_not_on_every_poll(self, netsh):
        """The tab re-reads its state every 3 seconds."""
        calls, _ = netsh
        for _ in range(5):
            reach.describe(8765)

        assert len(calls) == 1

    def test_another_port_is_its_own_question(self, netsh):
        calls, _ = netsh
        reach.describe(8765)
        reach.describe(9001)

        assert len(calls) == 2

    def test_off_windows_there_is_nothing_to_check(self, monkeypatch):
        monkeypatch.setattr(reach, "_platform", "darwin")
        reach.reset_cache()

        assert reach.describe(8765)["firewall"] == "not-applicable"


def test_it_states_what_protects_the_link(monkeypatch):
    monkeypatch.setattr(reach, "_addresses", lambda: [])
    security = reach.describe(8765)["security"]

    assert "TLS" in security and "token" in security
