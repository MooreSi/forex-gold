"""What httpx actually raises when a send is cut off, against a real socket.

`_send_failure` decides whether a lost answer is retried or parked, and it
decides it by exception type:

    _NEVER_SENT = (httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout)

Everything else is treated as unknown. That is a claim about httpx's behaviour
on a real network, and every other test of it constructs the exception by hand
-- which proves the branching and assumes the premise. If httpx raised
`ConnectError` for a mid-request disconnect, the classifier would call a lost
answer "never sent", the signal would be retried, and stage3/010's runaway is
back.

These stand up real listening sockets and break them in the two ways that
matter. No broker, no bridge, no order: the servers here answer nothing and
speak no MT5.

Written 2026-09-01 while working out how to run demo 2 (stage3/020) live. It
turned out the runbook's instruction -- stop the bridge first -- produces
`ConnectError`, which is the deliberately-safe branch, not the one under test.
"""
from __future__ import annotations

import asyncio
import socket
import threading

import httpx
import pytest

from backend.src.services.broker.mt5_client import _send_failure


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _Server:
    """A socket that accepts a connection and then behaves badly."""

    def __init__(self, mode: str):
        self.mode = mode
        self.port = _free_port()
        self._sock = socket.socket()
        self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self._sock.bind(("127.0.0.1", self.port))
        self._sock.listen(1)
        self.saw_request = threading.Event()
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _serve(self):
        try:
            conn, _ = self._sock.accept()
        except OSError:
            return
        with conn:
            try:
                conn.recv(65536)          # the request IS on the wire
                self.saw_request.set()
                if self.mode == "half_reply":
                    # Headers promising a body that never comes, then gone.
                    conn.sendall(b"HTTP/1.1 200 OK\r\nContent-Length: 99\r\n\r\n")
                # "drop": nothing at all, just close.
            except OSError:
                pass

    def close(self):
        try:
            self._sock.close()
        except OSError:
            pass


async def _post(port: int, timeout: float = 5.0) -> Exception:
    async with httpx.AsyncClient() as client:
        try:
            await client.post(f"http://127.0.0.1:{port}/order",
                              json={"direction": "BUY"}, timeout=timeout)
        except Exception as e:
            return e
    raise AssertionError("the request unexpectedly succeeded")


class TestAConnectionBrokenMidRequest:
    @pytest.mark.parametrize("mode", ["drop", "half_reply"])
    def test_it_is_classified_unknown(self, mode):
        """The order may be at the broker. The app cannot tell, and must not
        guess "no"."""
        server = _Server(mode)
        try:
            exc = asyncio.run(_post(server.port))
            assert server.saw_request.is_set(), "the request never reached the server"
        finally:
            server.close()

        out = _send_failure(exc)

        assert out.get("unknown") is True, (
            f"{type(exc).__name__} was treated as never-sent; a lost answer "
            f"would be retried and could become a second live order"
        )

    @pytest.mark.parametrize("mode", ["drop", "half_reply"])
    def test_it_is_not_one_of_the_never_sent_types(self, mode):
        """States the premise directly, so a failure names the assumption
        rather than the consequence."""
        from backend.src.services.broker.mt5_client import _NEVER_SENT

        server = _Server(mode)
        try:
            exc = asyncio.run(_post(server.port))
        finally:
            server.close()

        assert not isinstance(exc, _NEVER_SENT), type(exc).__name__


class TestNothingLeftTheMachine:
    def test_a_refused_connection_is_safe_to_retry(self):
        """Nothing was listening, so nothing was placed. Parking these would
        strand a signal every time the bridge restarts, and only
        reconciliation could release it.

        This is also what the runbook's demo-2 instruction actually produces:
        stopping the bridge first gives exactly this branch, not the one
        above.

        The assertion is on the CLASSIFICATION, not on the exception class.
        Asserting `httpx.ConnectError` pinned a detail of the operating system:
        macOS refuses the connection outright, Windows lets it time out, so
        this failed on CI (2026-09-02) with `ConnectTimeout` while the
        behaviour under test was identical and correct. `_NEVER_SENT` already
        lists both, so production classified the Windows case right all along
        -- only the test was platform-specific.
        """
        from backend.src.services.broker.mt5_client import _NEVER_SENT

        port = _free_port()          # bound, then released: nothing listening

        exc = asyncio.run(_post(port, timeout=2.0))

        assert isinstance(exc, _NEVER_SENT), type(exc).__name__
        assert _send_failure(exc).get("unknown") is None

    def test_the_two_cases_really_do_differ(self):
        """Negative control for the whole file. If both branches produced the
        same verdict, every test here would pass and the classifier would be
        doing nothing."""
        dead_port = _free_port()
        refused = asyncio.run(_post(dead_port, timeout=2.0))

        server = _Server("drop")
        try:
            cut_off = asyncio.run(_post(server.port))
        finally:
            server.close()

        assert _send_failure(refused).get("unknown") is None
        assert _send_failure(cut_off).get("unknown") is True


class TestTheErrorTextSurvives:
    def test_the_reason_is_carried_for_the_operator(self):
        server = _Server("drop")
        try:
            exc = asyncio.run(_post(server.port))
        finally:
            server.close()

        assert _send_failure(exc)["error"], "an empty error tells the operator nothing"
