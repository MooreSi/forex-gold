"""The admin server refuses to start on a machine that is not the admin machine.

`start()`'s docstring states the reason plainly: the external IP is confirmed
before binding "so the server never accidentally starts on a remote user's
machine even if they somehow obtained a copy of the admin password hash".

This is the server that issues licence keys, approves registrations and can
revoke a client. Started somewhere it should not be, the password is the only
thing left between a stranger and that authority. The gate was untested.

Nothing here opens a socket: `websockets.serve` is replaced, and the assertion
in every case is whether it was reached at all.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.remote import server as rs

pytestmark = pytest.mark.asyncio


@pytest.fixture
def harness(monkeypatch):
    """Everything start() touches, pointed away from the real install."""
    served: list = []
    tasks: list = []

    monkeypatch.setattr(rs, "_load_tokens", lambda: None)
    monkeypatch.setattr(rs, "_load_admin_machines", lambda: None)
    monkeypatch.setattr(rs, "resign_all_licences", lambda: {})
    monkeypatch.setattr(rs, "server_ssl_context", lambda: None)
    monkeypatch.setattr(rs, "_ping_loop", lambda: asyncio.sleep(0))
    monkeypatch.setattr(rs, "_lan_beacon_loop", lambda: asyncio.sleep(0))

    class _Server:
        async def wait_closed(self):
            return None

        def close(self):
            served.append("closed")

    async def _serve(*a, **kw):
        served.append("served")
        return _Server()

    import websockets
    monkeypatch.setattr(websockets, "serve", _serve)

    # get_RUNNING_loop, resolved lazily inside the call. Two traps here, both
    # hit on the way to this line: `rs.asyncio` IS the asyncio module, so
    # patching get_event_loop on it patches the global one and a _create_task
    # that calls get_event_loop() recurses forever; and resolving the loop at
    # fixture time fails outright, because this fixture is synchronous and no
    # loop is running yet. get_running_loop is not the function being patched
    # and by call time start() is executing inside the test's own loop.
    def _create_task(coro):
        t = asyncio.get_running_loop().create_task(coro)
        tasks.append(t)
        return t

    class _Loop:
        create_task = staticmethod(_create_task)

    monkeypatch.setattr(rs.asyncio, "get_event_loop", lambda: _Loop())
    return served, tasks


async def _settle(tasks):
    for t in tasks:
        try:
            await asyncio.wait_for(t, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass


def _ip_says(monkeypatch, is_admin: bool):
    async def _check():
        return is_admin
    monkeypatch.setattr(
        "backend.src.services.cluster.remote.ip_check.is_admin_machine", _check)


class TestTheIpGate:
    async def test_it_does_not_bind_on_a_machine_that_is_not_the_admin_machine(
        self, harness, monkeypatch,
    ):
        """The one that matters. This server issues licences."""
        served, tasks = harness
        _ip_says(monkeypatch, False)

        rs.start()
        await _settle(tasks)

        assert "served" not in served

    async def test_it_binds_on_the_admin_machine(self, harness, monkeypatch):
        """Negative control: a gate that refuses everything would satisfy the
        test above and break the product."""
        served, tasks = harness
        _ip_says(monkeypatch, True)

        rs.start()
        await _settle(tasks)

        assert "served" in served

    async def test_the_refusal_says_why(self, harness, monkeypatch, caplog):
        """An admin whose server silently does not start has nothing to go on.
        The message names the IP it expected."""
        import logging

        served, tasks = harness
        _ip_says(monkeypatch, False)

        with caplog.at_level(logging.WARNING):
            rs.start()
            await _settle(tasks)

        assert "not the admin machine" in caplog.text

    async def test_the_check_runs_before_the_ssl_context_is_built(
        self, harness, monkeypatch,
    ):
        """Ordering, read from the source: building the context on a
        non-admin machine writes a certificate and key into that machine's
        data directory. The gate must come first."""
        import pathlib

        src = pathlib.Path(rs.__file__).read_text(encoding="utf-8")
        body = src[src.index("async def _run():"):]

        assert body.index("is_admin_machine()") < body.index("server_ssl_context()")


class TestAFailureToBind:
    async def test_it_is_logged_rather_than_raised(self, harness, monkeypatch,
                                                   caplog):
        """start() is called from app startup. A port already in use must not
        take the whole app down with it."""
        import logging
        import websockets

        served, tasks = harness
        _ip_says(monkeypatch, True)

        async def _boom(*a, **kw):
            raise OSError("address already in use")
        monkeypatch.setattr(websockets, "serve", _boom)

        with caplog.at_level(logging.ERROR):
            rs.start()
            await _settle(tasks)

        assert "Failed to start" in caplog.text


class TestStop:
    async def test_it_closes_the_server_and_clears_the_handle(self, harness,
                                                              monkeypatch):
        served, tasks = harness
        _ip_says(monkeypatch, True)
        rs.start()
        await _settle(tasks)

        rs.stop()

        assert "closed" in served
        assert rs._server_obj is None

    async def test_stopping_twice_is_harmless(self, harness, monkeypatch):
        """Called from shutdown paths that can run more than once."""
        served, tasks = harness
        _ip_says(monkeypatch, True)
        rs.start()
        await _settle(tasks)

        rs.stop()
        rs.stop()

    async def test_stopping_something_never_started_is_harmless(self):
        rs._server_obj = None
        rs._server_task = None

        rs.stop()
