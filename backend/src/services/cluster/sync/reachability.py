"""Can the paired machine reach this VPS? What Settings > Remote node shows.

Read-only: it never opens a connection to anywhere and never changes the
firewall. The installer creates the rule (FOREX_Trader_Setup.iss, Step 4); this
only reports whether it is there and, if not, the command that adds it.
"""
from __future__ import annotations

import ipaddress
import socket
import subprocess
import sys
import time

RULE_NAME = "FOREX Trader Sync (port {port})"
_FIREWALL_TTL_SECS = 60.0

SECURITY = (
    "Encrypted with TLS using this machine's own certificate. The other machine "
    "pins that certificate on its first connection and refuses any other after, "
    "and every connection must present the shared pairing token."
)

# Seams, so a test can be Windows with a scripted netsh.
_platform = sys.platform
_run = subprocess.run
_firewall_cache: dict[int, tuple[float, str]] = {}


def reset_cache() -> None:
    _firewall_cache.clear()


def _addresses() -> list[str]:
    """This machine's IPv4 addresses, the outbound one first, loopback never.

    The UDP connect sends nothing; it only makes the OS pick the interface it
    would route through, which is the address a peer is most likely to reach.
    """
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("192.0.2.1", 9))
            found.append(s.getsockname()[0])
    except OSError:
        pass
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            found.append(info[4][0])
    except OSError:
        pass
    out: list[str] = []
    for addr in found:
        if not addr.startswith("127.") and addr != "0.0.0.0" and addr not in out:
            out.append(addr)
    return out


def firewall_command(port: int) -> str:
    return (f'netsh advfirewall firewall add rule name="{RULE_NAME.format(port=port)}" '
            f"dir=in action=allow protocol=TCP localport={port}")


def _firewall(port: int) -> str:
    """"open", "missing", "unknown", or "not-applicable" off Windows."""
    if _platform != "win32":
        return "not-applicable"
    cached = _firewall_cache.get(port)
    if cached and time.monotonic() - cached[0] < _FIREWALL_TTL_SECS:
        return cached[1]
    try:
        proc = _run(
            ["netsh", "advfirewall", "firewall", "show", "rule",
             f"name={RULE_NAME.format(port=port)}"],
            capture_output=True, text=True, timeout=10,
        )
        state = "open" if proc.returncode == 0 else "missing"
    except Exception:
        state = "unknown"
    _firewall_cache[port] = (time.monotonic(), state)
    return state


def describe(port: int) -> dict:
    addresses = _addresses()
    return {
        "addresses": addresses,
        "behind_nat": bool(addresses) and all(
            ipaddress.ip_address(a).is_private for a in addresses),
        "firewall": _firewall(port),
        "firewall_command": firewall_command(port),
        "security": SECURITY,
    }
