"""A sync STAND_DOWN pauses engines; it never records them as user-stopped.

Found on the owner's VPS, 2026-09-28. At 2026-09-25 23:19:31 a STAND_DOWN
stopped the Reversal Engine with `stop()` -- whose default is persist=True,
which writes re_user_stopped=1, the flag that means "the user switched it
off". The next RESUME restarted nothing, and every app start from then on
logged "Reversal Engine skipped (user manually stopped)". The VPS ran for
three days without the engine and nobody had chosen that.

App shutdown already calls stop(persist=False) for exactly this reason. A
stand-down is the same kind of stop: the node decides, not the user.

Nothing here reaches a broker, a database or a socket: the engines are fakes,
db_module is replaced by a recorder, and the websocket is a list.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync import protocol as P

pytestmark = pytest.mark.asyncio


class _PersistingEngine:
    """Shaped like ReversalEngineService: stop(persist=True) by default."""

    def __init__(self):
        self.is_running = True
        self.stop_calls: list[bool] = []

    def stop(self, persist: bool = True) -> None:
        self.stop_calls.append(persist)
        self.is_running = False


class _PlainEngine:
    """Shaped like the breakout engine: stop() takes nothing."""

    def __init__(self):
        self.is_running = True
        self.stopped = 0

    def stop(self) -> None:
        self.stopped += 1
        self.is_running = False


class _FakeDb:
    def __init__(self):
        self.stood_down = []
        self.active = P.TRADER_REMOTE_VPS

    def get_active_trader(self):
        return self.active

    def set_active_trader(self, v):
        self.active = v

    def get_stood_down_engines(self):
        return list(self.stood_down)

    def set_stood_down_engines(self, names):
        self.stood_down = list(names)


class _Ws:
    def __init__(self):
        self.sent = []

    async def send(self, raw):
        self.sent.append(raw)


def _server(re_eng, bo_eng):
    srv = ss.SyncServer.__new__(ss.SyncServer)
    srv._main_engine = None
    srv._breakout_engine = bo_eng
    srv._bounce_engine = None
    srv._re_engine = re_eng
    return srv


async def test_reversal_engine_is_stopped_without_persisting(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr(ss, "db_module", fake_db)
    re_eng, bo_eng = _PersistingEngine(), _PlainEngine()

    await _server(re_eng, bo_eng)._handle_stand_down(_Ws())

    assert re_eng.stop_calls == [False]
    assert not re_eng.is_running


async def test_an_engine_without_persist_still_stops(monkeypatch):
    fake_db = _FakeDb()
    monkeypatch.setattr(ss, "db_module", fake_db)
    re_eng, bo_eng = _PersistingEngine(), _PlainEngine()

    await _server(re_eng, bo_eng)._handle_stand_down(_Ws())

    assert bo_eng.stopped == 1
    assert sorted(fake_db.stood_down) == ["breakout", "reversal_engine"]
