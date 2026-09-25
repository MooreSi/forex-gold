"""Starting the sync server when one is already listening.

Reported 2026-09-25 on a VPS: "Make this node a VPS" failed with
`[Errno 10048] ... only one usage of each socket address is normally
permitted`, while the tab beside it said "listening". The app had started a
server at boot (sync_server_enabled was on from 6.1), and pressing the button
built a SECOND one and tried to bind the same port. The failure then recorded
the setting as off while the first server went on listening.

Also here: a stopped server kept its heartbeat and liveness loops running, so
a machine that had stopped being the VPS could still send "Mac unreachable"
alerts from a server nobody could reach.

`websockets.serve` is replaced by a fake that refuses a port already bound, as
the OS does; no socket is opened and no certificate is written.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.sync import server as srv_mod
from backend.src.services.cluster.sync import tls_util


class _Bound:
    def __init__(self, ports, port):
        self.ports, self.port = ports, port

    def close(self):
        self.ports.discard(self.port)

    async def wait_closed(self):
        return None


@pytest.fixture
def os_ports(monkeypatch):
    """A port table: binding one that is taken raises like Windows does."""
    import websockets

    ports: set[int] = set()

    async def _serve(handler, host, port, **kw):
        if port in ports:
            raise OSError(10048, "only one usage of each socket address")
        ports.add(port)
        return _Bound(ports, port)

    async def _idle(self):
        await asyncio.sleep(3600)

    monkeypatch.setattr(websockets, "serve", _serve)
    monkeypatch.setattr(tls_util, "server_ssl_context", lambda host: None)
    for loop in ("_heartbeat_loop", "_signal_gen_stats_loop", "_liveness_watchdog_loop"):
        monkeypatch.setattr(srv_mod.SyncServer, loop, _idle)
    monkeypatch.setattr(srv_mod, "_instance", None)
    monkeypatch.setattr(srv_mod, "_listening", None, raising=False)
    return ports


def test_a_second_start_replaces_the_one_listening(os_ports):
    async def _go():
        first = srv_mod.init()
        await first.start("0.0.0.0", 8765, "tok")
        second = srv_mod.init()
        await second.start("0.0.0.0", 8765, "tok")
        return first, second

    first, second = asyncio.run(_go())

    assert os_ports == {8765}
    assert second.is_listening and not first.is_listening


def test_stopping_cancels_the_servers_own_loops(os_ports):
    """Otherwise a machine that stopped being the VPS keeps alerting."""
    async def _go():
        s = srv_mod.init()
        await s.start("0.0.0.0", 8765, "tok")
        tasks = list(s._tasks)
        await s.stop()
        await asyncio.sleep(0)
        return tasks

    tasks = asyncio.run(_go())

    assert tasks and all(t.cancelled() for t in tasks)


def test_listening_is_false_after_stop_although_the_server_still_exists(os_ports):
    """get_instance() stays set after a stop on purpose: several trading paths
    read "an instance exists" as "this is the VPS", and changing that is not
    this fix. Whether it is LISTENING is a separate question."""
    async def _go():
        s = srv_mod.init()
        await s.start("0.0.0.0", 8765, "tok")
        assert srv_mod.is_listening() is True
        await s.stop()
        return s

    s = asyncio.run(_go())

    assert srv_mod.get_instance() is s
    assert srv_mod.is_listening() is False
    assert os_ports == set()


def test_stopping_an_old_server_leaves_the_new_one_listening(os_ports):
    async def _go():
        old = srv_mod.init()
        await old.start("0.0.0.0", 8765, "tok")
        new = srv_mod.init()
        await new.start("0.0.0.0", 8765, "tok")
        await old.stop()
        return new

    new = asyncio.run(_go())

    assert srv_mod.is_listening() is True and new.is_listening
