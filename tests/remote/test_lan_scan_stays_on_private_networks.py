"""The LAN-discovery scan must never sweep a network the machine does not own.

_scan_lan_for_server() probes every host on the client's local /24 for port
8443. That is right on a home LAN, where the admin server is a neighbour. On a
VPS the machine's own address is a public one, so the "local /24" is the
hosting provider's other customers.

Confirmed live 2026-10-12: the app on the owner's VPS (38.253.124.25) could not
reach the admin server, fell back to the scan on every retry, and made about
3,000 connection attempts to other customers' servers on port 8443 between
15:52 and 16:12 UTC. The provider's security systems cut the VPS off the
network -- SSH, WinRM and RDP included -- and kept an outbound 8443 block in
place until the scan was gone.

The unit tests pin the address rule; the end-to-end ones run the real scan with
the TCP connect replaced by a counter, so they see every probe it would send.
"""
import asyncio

import pytest

from backend.src.services.cluster.remote import client as rc

VPS_PUBLIC_IP = "38.253.124.25"


class _ProbeCounter:
    """Stands in for asyncio.open_connection and records each host probed."""

    def __init__(self):
        self.hosts = []

    async def __call__(self, host, port, *args, **kwargs):
        self.hosts.append(host)
        raise OSError("probe refused")


@pytest.fixture
def probes(monkeypatch):
    counter = _ProbeCounter()
    monkeypatch.setattr(rc.asyncio, "open_connection", counter)
    return counter


@pytest.mark.parametrize("ip", [
    VPS_PUBLIC_IP,
    "8.8.8.8",
    "217.155.25.160",
    "",
    "not an address",
])
def test_scan_is_refused_for_a_public_or_unknown_address(ip):
    from backend.src.services.cluster.remote import _lan_scan
    assert _lan_scan.is_scannable(ip) is False


@pytest.mark.parametrize("ip", [
    "192.168.0.14",
    "10.30.17.85",
    "172.16.4.2",
    "127.0.0.2",
])
def test_scan_is_allowed_for_a_private_or_loopback_address(ip):
    from backend.src.services.cluster.remote import _lan_scan
    assert _lan_scan.is_scannable(ip) is True


def test_scan_sends_no_probes_from_a_public_address(monkeypatch, probes):
    """The live failure: a VPS with its public IP on the adapter."""
    monkeypatch.setattr(rc, "_get_local_ip", lambda: VPS_PUBLIC_IP)
    assert asyncio.run(rc._scan_lan_for_server()) == ""
    assert probes.hosts == []


def test_scan_still_sweeps_the_local_subnet_on_a_private_address(monkeypatch, probes):
    """Negative control: the counter does see probes, and the scan still works
    where it is meant to, so the test above cannot pass by the scan being dead."""
    monkeypatch.setattr(rc, "_get_local_ip", lambda: "192.168.0.14")
    assert asyncio.run(rc._scan_lan_for_server()) == ""
    assert len(probes.hosts) == 254
    assert all(h.startswith("192.168.0.") for h in probes.hosts)
