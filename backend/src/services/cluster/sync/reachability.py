"""Can the paired machine reach this VPS? What Settings > Remote node shows,
and the firewall rule "Make this node a VPS" adds and "Stop being a VPS"
removes.

The installer deliberately does not open the port (owner, 2026-09-25): many
people run the app on their main Windows PC with no VPS, and that machine
should accept nothing inbound. `describe` only reads; `open_port` and
`close_port` change the Windows firewall and run only on those two presses.
Nothing here opens a connection to anywhere.
"""
from __future__ import annotations

import asyncio
import base64
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


def _netsh_args(verb: str, port: int) -> str:
    """The netsh arguments as one string. On Windows a str goes to
    CreateProcess verbatim, so `name="..."` reaches netsh quoted as written;
    a list would be re-quoted as `"name=..."`."""
    args = f'advfirewall firewall {verb} rule name="{RULE_NAME.format(port=port)}"'
    if verb == "add":
        args += f" dir=in action=allow protocol=TCP localport={port}"
    return args


def firewall_command(port: int) -> str:
    return "netsh " + _netsh_args("add", port)


def _netsh(verb: str, port: int, timeout: float = 10) -> int:
    return _run("netsh " + _netsh_args(verb, port),
                capture_output=True, text=True, timeout=timeout).returncode


def _netsh_elevated(verb: str, port: int) -> int:
    """Run netsh through a UAC prompt. Encoded, so no layer re-quotes it; the
    wait covers the operator reading the prompt. Declining exits non-zero."""
    inner = _netsh_args(verb, port).replace("'", "''")
    script = (f"$p = Start-Process -FilePath netsh -ArgumentList '{inner}' "
              "-Verb RunAs -Wait -PassThru -WindowStyle Hidden; exit $p.ExitCode")
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    return _run(["powershell", "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
                capture_output=True, text=True, timeout=180).returncode


def _change(verb: str, port: int, done: str) -> str:
    """Try plainly (a VPS usually runs as the built-in Administrator), then
    elevated. Returns `done`, "declined", or "not-applicable" off Windows."""
    if _platform != "win32":
        return "not-applicable"
    want_rule = verb == "add"
    if (_firewall(port) == "open") == want_rule:
        return done
    try:
        ok = _netsh(verb, port) == 0 or _netsh_elevated(verb, port) == 0
    except Exception:
        ok = False
    _firewall_cache.pop(port, None)
    return done if ok else "declined"


def open_port(port: int) -> str:
    return _change("add", port, "open")


def close_port(port: int) -> str:
    return _change("delete", port, "closed")


async def open_port_async(port: int) -> str:
    """Off the event loop: a UAC prompt waits for a person."""
    return await asyncio.to_thread(open_port, port)


async def close_port_async(port: int) -> str:
    return await asyncio.to_thread(close_port, port)


def _firewall(port: int) -> str:
    """"open", "missing", "unknown", or "not-applicable" off Windows."""
    if _platform != "win32":
        return "not-applicable"
    cached = _firewall_cache.get(port)
    if cached and time.monotonic() - cached[0] < _FIREWALL_TTL_SECS:
        return cached[1]
    try:
        state = "open" if _netsh("show", port) == 0 else "missing"
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
