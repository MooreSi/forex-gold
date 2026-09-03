"""LAN discovery must not mistake any open port 8443 for the admin server.

_scan_lan_for_server() used to accept the first host on the client's /24 with
SERVER_PORT open, with no validation at all. 8443 is a common alternate-HTTPS
port, so a router, NAS or hypervisor answers that probe exactly as readily as
the admin server does -- and because rediscovery runs on every retry, the
client locked onto the wrong host permanently and never fell back to the WAN
address.

Confirmed live 2026-08-07: a remote Mac sat on the activation screen reporting
"server rejected WebSocket connection: HTTP 404" while the real admin server
was up and reachable the whole time. Its registration could never be sent,
because the client sends one only in reply to the real server's reject.

These tests use real sockets on loopback: one genuine WebSocket server, one
TLS server that answers HTTP 404 to everything (what the router did).
"""
import asyncio
import http.server
import socket
import ssl
import threading
import time

import pytest
import websockets

from backend.src.services.cluster.remote import client as rc
from backend.src.services.cluster.remote.tls import ensure_cert


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def certs():
    return ensure_cert()


class _NotOurServer(http.server.BaseHTTPRequestHandler):
    """Answers 404 to everything, including a WebSocket upgrade request."""

    def do_GET(self):
        self.send_response(404)
        self.end_headers()

    def log_message(self, *args):
        pass


@pytest.fixture
def https_404_server(certs):
    """A TLS server on a spare port that is emphatically not the admin server."""
    cert_file, key_file = certs
    port = _free_port()
    httpd = http.server.HTTPServer(("127.0.0.1", port), _NotOurServer)
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_file), str(key_file))
    httpd.socket = ctx.wrap_socket(httpd.socket, server_side=True)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield port
    httpd.shutdown()
    httpd.server_close()


@pytest.fixture
def real_ws_server(certs):
    """A genuine WebSocket server, as the admin server presents itself."""
    cert_file, key_file = certs
    port = _free_port()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(str(cert_file), str(key_file))
    holder = {}

    def _run():
        async def _serve():
            async def _handler(ws):
                try:
                    async for _ in ws:
                        pass
                except Exception:
                    pass
            async with websockets.serve(_handler, "127.0.0.1", port, ssl=ctx):
                await holder["stop"]
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        holder["stop"] = loop.create_future()
        holder["loop"] = loop
        loop.run_until_complete(_serve())

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    for _ in range(100):
        if "loop" in holder:
            break
        threading.Event().wait(0.05)
    # Poll with a real connect instead of a flat sleep -- proves the server
    # is actually accepting before the test proceeds, rather than guessing
    # how long websockets.serve() takes to bind under CI-runner contention.
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            threading.Event().wait(0.05)
    yield port
    holder["loop"].call_soon_threadsafe(holder["stop"].set_result, None)


def test_open_port_that_is_not_our_server_is_rejected(monkeypatch, https_404_server):
    """The exact live failure: TLS answers, HTTP 404 to the upgrade."""
    monkeypatch.setattr(rc, "SERVER_PORT", https_404_server)
    assert asyncio.run(rc._speaks_our_protocol("127.0.0.1")) is False


def test_real_websocket_server_is_accepted(monkeypatch, real_ws_server):
    monkeypatch.setattr(rc, "SERVER_PORT", real_ws_server)
    assert asyncio.run(rc._speaks_our_protocol("127.0.0.1")) is True


def test_closed_port_is_rejected(monkeypatch):
    monkeypatch.setattr(rc, "SERVER_PORT", _free_port())
    assert asyncio.run(rc._speaks_our_protocol("127.0.0.1")) is False


def test_scan_ignores_a_host_that_only_has_the_port_open(monkeypatch, https_404_server):
    """End to end through the real subnet scan: the impostor is shortlisted by
    the TCP probe and then discarded, so the scan returns '' and the connect
    loop falls back to the WAN address instead of locking onto it.

    Reporting the local IP as 127.0.0.2 makes the scan sweep 127.0.0.0/24,
    where only 127.0.0.1 accepts a connection -- the 404 server, standing in
    for the router that caused the live failure.
    """
    monkeypatch.setattr(rc, "SERVER_PORT", https_404_server)
    monkeypatch.setattr(rc, "_get_local_ip", lambda: "127.0.0.2")
    # 254 concurrent connects sharing one event loop is real CI-runner
    # contention, not something this test is trying to measure -- give each
    # probe more headroom than production's real-LAN default (see
    # _SCAN_PROBE_TIMEOUT_S's docstring).
    monkeypatch.setattr(rc, "_SCAN_PROBE_TIMEOUT_S", 3.0)

    assert asyncio.run(rc._scan_lan_for_server()) == ""


def test_scan_returns_a_host_that_really_is_the_server(monkeypatch, real_ws_server):
    """The same sweep must still find the genuine server -- the validation
    tightens discovery, it does not disable it."""
    monkeypatch.setattr(rc, "SERVER_PORT", real_ws_server)
    monkeypatch.setattr(rc, "_get_local_ip", lambda: "127.0.0.2")
    monkeypatch.setattr(rc, "_SCAN_PROBE_TIMEOUT_S", 3.0)

    assert asyncio.run(rc._scan_lan_for_server()) == "127.0.0.1"
