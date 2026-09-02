"""Finding the admin server on the local network.

The client prefers a LAN address over the hardcoded WAN one, because most home
routers do not hairpin NAT: a machine on the same network as the admin server
often cannot reach it via its public IP at all. `_blocking_probe_beacon`
listens for the server's UDP broadcast and returns the local address it
advertises.

Two properties carry the whole feature, and neither is visible to a mock:

  * **`SO_BROADCAST` is required on Windows** to receive broadcast packets --
    without it the OS drops them before the socket sees them. A mocked socket
    cannot tell you that, which is why these use real UDP on the loopback.
  * **A beacon advertising the WAN address is ignored.** Taking it would send
    the client back through the router it is trying to avoid.

And the failure everything else depends on: a stray or malformed packet on
that port must not end the listen. The port is not exclusively ours, and one
unparseable datagram ending discovery would silently push every LAN client
onto the WAN path.

Real sockets, loopback only, nothing leaves the machine.
"""
from __future__ import annotations

import json
import socket
import threading
import time

import pytest

from backend.src.services.cluster.remote import client as rc


def _probe_code() -> str:
    """The probe's CODE, with its docstring and comments stripped.

    The docstring explains why SO_BROADCAST is needed, so a naive substring
    search over the whole function matches the explanation rather than the
    call -- and passes with the line deleted. Mutation found that.
    """
    import ast
    import pathlib

    src = pathlib.Path(rc.__file__).read_text(encoding="utf-8")
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "_blocking_probe_beacon")
    body = fn.body[1:] if (fn.body and isinstance(fn.body[0], ast.Expr)
                           and isinstance(fn.body[0].value, ast.Constant)) else fn.body
    return "\n".join(ast.unparse(node) for node in body)


@pytest.fixture(autouse=True)
def own_port(monkeypatch):
    """Give every test its own beacon port.

    They all bound the one real port (8444) and `_beacon` fires from a delayed
    daemon thread -- so a test that finished early delivered its datagram into
    the NEXT test's socket. That made the file flaky: 4 failures, then 11
    passes, then 4 failures, on unchanged code. A flaky test is worse than no
    test, because it teaches you to re-run rather than to look.

    `_blocking_probe_beacon` reads the module global at call time, so patching
    it here is enough.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    monkeypatch.setattr(rc, "_LAN_BEACON_PORT", port)
    return port


def _beacon(payload, delay=0.05, port=None):
    """Send one UDP datagram to this test's beacon port after a short delay."""
    target = port or rc._LAN_BEACON_PORT

    def _send():
        time.sleep(delay)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            s.sendto(raw, ("127.0.0.1", target))
    t = threading.Thread(target=_send, daemon=True)
    t.start()
    return t


class TestHearingABeacon:
    def test_it_returns_the_advertised_lan_address(self):
        _beacon({"type": "forex_admin_beacon", "ip": "192.168.1.50", "port": 8443})

        assert rc._blocking_probe_beacon(2.0) == "192.168.1.50"

    def test_the_wan_address_is_ignored(self):
        """Taking it would send the client back through the router it is
        trying to avoid -- the entire reason this discovery exists."""
        _beacon({"type": "forex_admin_beacon", "ip": rc.SERVER_HOST})

        assert rc._blocking_probe_beacon(0.6) == ""

    def test_a_beacon_with_no_address_is_ignored(self):
        """A valid beacon follows it, so "ignored" means "kept listening"
        rather than "returned empty".

        Asserting only that the result is "" cannot fail: accepting the empty
        address returns "" too. Mutation found exactly that -- the two
        outcomes are the same string and only the second beacon separates
        them.
        """
        _beacon({"type": "forex_admin_beacon", "ip": ""}, delay=0.05)
        _beacon({"type": "forex_admin_beacon", "ip": "192.168.1.52"}, delay=0.35)

        assert rc._blocking_probe_beacon(3.0) == "192.168.1.52"

    def test_someone_else_s_broadcast_is_ignored(self):
        """This port is not exclusively ours."""
        _beacon({"type": "some_other_app", "ip": "192.168.1.99"})

        assert rc._blocking_probe_beacon(0.6) == ""


class TestNoiseOnThePort:
    def test_a_malformed_packet_does_not_end_the_listen(self):
        """The property everything else rests on. One unparseable datagram
        ending discovery would silently push every LAN client onto the WAN
        path, where most home routers cannot reach the server at all."""
        _beacon(b"not json at all", delay=0.05)
        _beacon({"type": "forex_admin_beacon", "ip": "192.168.1.50"}, delay=0.35)

        assert rc._blocking_probe_beacon(3.0) == "192.168.1.50"

    def test_several_junk_packets_do_not_end_it(self):
        for i in range(4):
            _beacon(b"\xff\xfe garbage", delay=0.05 + i * 0.05)
        _beacon({"type": "forex_admin_beacon", "ip": "192.168.1.51"}, delay=0.45)

        assert rc._blocking_probe_beacon(3.0) == "192.168.1.51"


class TestHearingNothing:
    def test_it_gives_up_and_returns_empty(self):
        assert rc._blocking_probe_beacon(0.3) == ""

    def test_it_does_not_wait_much_longer_than_asked(self):
        """The connect loop calls this on every reconnect attempt. A probe
        that overruns its timeout delays every retry behind it."""
        started = time.monotonic()

        rc._blocking_probe_beacon(0.3)

        assert time.monotonic() - started < 2.0


class TestTheSocketOptions:
    def test_broadcast_is_enabled(self):
        """Required on Windows to receive broadcast packets at all; without
        it the OS drops them before the socket sees them. Read from the
        source, because on macOS -- where these tests run -- loopback unicast
        arrives with or without it, so no behavioural test here can fail."""
        import pathlib

        probe = _probe_code()

        assert "SO_BROADCAST" in probe

    def test_the_address_is_reusable(self):
        """Without SO_REUSEADDR a second probe after an abrupt close cannot
        bind, and discovery fails until the OS releases the port."""
        import pathlib

        probe = _probe_code()

        assert "SO_REUSEADDR" in probe

    def test_a_socket_failure_is_survivable(self, monkeypatch):
        """It must return "" rather than raise: this runs on the connect
        path, and an exception here would break reconnection entirely.

        The failure is forced at socket creation. An earlier version bound
        port 1 expecting it to be privileged -- on macOS a non-root process
        binds UDP port 1 quite happily, so nothing failed and the test proved
        nothing.
        """
        def _boom(*a, **kw):
            raise OSError("no sockets today")
        monkeypatch.setattr(rc.socket, "socket", _boom)

        assert rc._blocking_probe_beacon(0.2) == ""
