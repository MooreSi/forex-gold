"""EABridge listens on every port a chart-persisted InpPort might name.

MetaTrader stores the EA's InpPort in the chart file and a persisted input
always beats the recompiled default. A terminal that crashes never writes its
charts, so the restart restores an older input -- which on 2026-08-07 left the
EA dialling 9101 while this process listened on 9111 for four hours.

No MT5 order is ever placed, closed, or modified by any of this.
"""
import asyncio
import socket

import pytest

from forex_trader.core import ea_bridge


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def test_listen_ports_puts_the_configured_port_first():
    ports = ea_bridge.listen_ports()
    assert ports[0] == ea_bridge._PORT
    for legacy in ea_bridge._LEGACY_PORTS:
        assert legacy in ports


def test_listen_ports_never_repeats_a_port():
    assert len(ea_bridge.listen_ports()) == len(set(ea_bridge.listen_ports()))


def test_binds_every_listed_port_and_releases_them_on_stop(monkeypatch):
    primary, legacy = _free_port(), _free_port()
    monkeypatch.setattr(ea_bridge, "_PORT", primary)
    monkeypatch.setattr(ea_bridge, "_LEGACY_PORTS", (legacy,))

    async def _run():
        bridge = ea_bridge.EABridge(engine=None)
        await bridge.start()
        assert bridge.listening_ports() == sorted([primary, legacy])
        await bridge.stop()
        return bridge

    bridge = asyncio.run(_run())
    assert bridge.listening_ports() == []
    # Both ports are genuinely free again.
    for p in (primary, legacy):
        s = socket.socket()
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", p))
        s.close()


def test_a_legacy_port_held_by_something_else_is_not_fatal(monkeypatch):
    """A second copy of the app, or a leftover from a crash, must not stop the
    bridge starting -- the configured port is what a current EA dials."""
    primary, legacy = _free_port(), _free_port()
    monkeypatch.setattr(ea_bridge, "_PORT", primary)
    monkeypatch.setattr(ea_bridge, "_LEGACY_PORTS", (legacy,))

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", legacy))
    squatter.listen(1)

    async def _run():
        bridge = ea_bridge.EABridge(engine=None)
        await bridge.start()
        assert bridge.listening_ports() == [primary]
        return bridge

    bridge = asyncio.run(_run())
    squatter.close()
    asyncio.run(bridge.stop())


def test_failing_to_bind_the_configured_port_still_raises(monkeypatch):
    """Silently running without the port a current EA dials is exactly the
    failure this whole change is about -- it must stay loud."""
    primary = _free_port()
    monkeypatch.setattr(ea_bridge, "_PORT", primary)
    monkeypatch.setattr(ea_bridge, "_LEGACY_PORTS", ())

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", primary))
    squatter.listen(1)

    async def _run():
        bridge = ea_bridge.EABridge(engine=None)
        with pytest.raises(OSError):
            await bridge.start()

    try:
        asyncio.run(_run())
    finally:
        squatter.close()


def test_bind_ports_is_idempotent_and_picks_up_a_port_freed_later(monkeypatch):
    """What the EA link watchdog calls on every cycle while the link is down."""
    primary, legacy = _free_port(), _free_port()
    monkeypatch.setattr(ea_bridge, "_PORT", primary)
    monkeypatch.setattr(ea_bridge, "_LEGACY_PORTS", (legacy,))

    squatter = socket.socket()
    squatter.bind(("127.0.0.1", legacy))
    squatter.listen(1)

    async def _run():
        bridge = ea_bridge.EABridge(engine=None)
        await bridge.start()
        assert bridge.listening_ports() == [primary]

        # Re-binding while nothing has changed claims nothing new.
        assert await bridge.bind_ports() == []

        squatter.close()
        assert await bridge.bind_ports() == [legacy]
        assert bridge.listening_ports() == sorted([primary, legacy])
        await bridge.stop()

    try:
        asyncio.run(_run())
    finally:
        squatter.close()


def test_an_ea_on_a_legacy_port_reaches_the_bridge_and_is_flagged(monkeypatch, caplog):
    """The end-to-end point of the change: a stale InpPort still gets a live
    link, and the drift that caused it is reported rather than hidden."""
    primary, legacy = _free_port(), _free_port()
    monkeypatch.setattr(ea_bridge, "_PORT", primary)
    monkeypatch.setattr(ea_bridge, "_LEGACY_PORTS", (legacy,))

    async def _run():
        bridge = ea_bridge.EABridge(engine=None)
        await bridge.start()
        reader, writer = await asyncio.open_connection("127.0.0.1", legacy)
        # The EA's own 2s heartbeat is what keeps this fresh; one ping is
        # enough to prove the connection was accepted and read.
        writer.write(b'{"type":"ping"}\n')
        await writer.drain()
        assert await reader.readline() == b'{"type": "pong"}\n'
        assert bridge.is_ea_healthy() is True
        assert bridge.last_connected_at > 0
        writer.close()
        await bridge.stop()

    with caplog.at_level("WARNING", logger="ea_bridge"):
        asyncio.run(_run())

    assert any("fallback port" in r.message for r in caplog.records), caplog.text
