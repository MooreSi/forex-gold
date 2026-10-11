"""Bounce is gone from the protocol too, and a peer that still names it is refused.

The engine's code was deleted on 2026-09-14 and it left the screen on
2026-09-19. Its NAME stayed behind in the engine registry, the sync server,
the telemetry payload and the AI-eval table, on the stated ground that the
registry's order was "the wire order": the sync server bound the engines
positionally, so dropping the slot would shift Reversal into Bounce's place on
a paired node running an older build.

That was checked on 2026-09-28 and it does not hold. The position only ever
existed inside one process -- `sub_engines()` is unpacked by
`api/routers/remote.py` and handed to `server_start` by keyword. Everything
that crosses the wire is keyed by NAME: `engine_control` carries
`{"engine": "<name>"}`, the heartbeat's `engines` is a dict, the stood-down
list is a list of names, and every build since the first commit reads the
`signal_gen_stats` payload with `.get(key, {})`, so a missing key reads
exactly like the empty dict this node was sending.

So the name goes, and what has to hold instead is the compatibility property:
**a paired node on an older build that still says "bounce" gets a refusal it
can show, and nothing on this node starts, crashes or writes a setting.**

Nothing here reaches a broker, a database or a socket: the engines are fakes
that record what was called, the websocket is a list, and the database module
is replaced by a recorder.
"""
from __future__ import annotations

import asyncio
import inspect
import json

import pytest

from backend.src.services.cluster.sync import server as ss
from backend.src.services.cluster.sync.protocol import MSG_ENGINE_CONTROL_ACK
from backend.src.services.engines import registry


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))


class _Engine:
    def __init__(self, running=False):
        self.is_running = running
        self.calls: list = []

    def start(self):
        self.calls.append("start")
        self.is_running = True

    def stop(self):
        self.calls.append("stop")
        self.is_running = False

    async def _run_cycle(self):
        self.calls.append("run_now")


@pytest.fixture
def node():
    """A real SyncServer with its REAL `_sub_engines`, so the name table under
    test is the one production uses rather than one the test wrote."""
    bo, re_ = _Engine(), _Engine()
    srv = ss.SyncServer(breakout_engine=bo, re_engine=re_)
    srv.fakes = {"breakout": bo, "reversal_engine": re_}
    return srv


@pytest.fixture
def settings_writes(monkeypatch):
    writes: list = []
    from backend.src.db import database as _db
    monkeypatch.setattr(_db, "update_risk_settings",
                        lambda fields, **kw: writes.append(fields))
    return writes


# ── this build no longer names it ────────────────────────────────────────────

class TestTheRegistryNamesOnlyWhatExists:

    def test_the_names_are_the_two_engines_this_build_has(self):
        assert registry.ENGINE_NAMES == ("breakout", "reversal")

    def test_every_name_has_a_service(self):
        # The empty slot was the only entry without one. An entry bound to
        # None is how a name for deleted code survives.
        assert all(svc is not None for svc in registry._ENGINE_SERVICES.values())

    def test_all_instances_is_one_per_engine(self):
        assert len(registry.all_instances()) == 2

    def test_the_controller_hands_out_two(self):
        from backend.src.controllers import engines_controller as ec
        assert len(ec.sub_engines()) == 2
        assert "bounce" not in ec.engines_running()


class TestTheSyncServerNoLongerTakesIt:

    def test_the_server_has_no_bounce_parameter(self):
        assert "bounce_engine" not in inspect.signature(ss.SyncServer).parameters
        assert "bounce_engine" not in inspect.signature(ss.init).parameters

    def test_the_controller_has_no_bounce_parameter(self):
        from backend.src.controllers import sync_controller
        assert "bounce_engine" not in inspect.signature(
            sync_controller.server_start).parameters

    def test_its_engines_are_the_two_that_exist(self, node):
        # These keys go into every heartbeat, and the Mac's Remote Node page
        # printed "Bounce off" from this dict.
        #
        # CHANGED 2026-10-11 (owner): Trend PA joined main by name after this
        # test was written; the property is that "bounce" is absent.
        assert set(node._sub_engines()) == {"breakout", "reversal_engine", "trend_pa"}

    def test_the_stats_payload_has_no_bounce_key(self, node, monkeypatch):
        monkeypatch.setattr(type(node), "_breakout_stats", staticmethod(lambda: {"b": 1}))
        monkeypatch.setattr(type(node), "_reversal_engine_stats",
                            staticmethod(lambda: {"r": 1}))
        monkeypatch.setattr(type(node), "_trend_pa_stats",
                            staticmethod(lambda: {"t": 1}))

        payload = asyncio.run(node._signal_gen_stats_payload())

        # CHANGED 2026-10-11 (owner): trend_pa joined the payload on main.
        assert set(payload) == {"breakout", "reversal_engine", "trend_pa"}


class TestTheAiEvalTableHasOnlyBreakout:

    def test_the_remote_control_table(self):
        from backend.src.services.cluster import remote_control as rc
        assert rc.AI_EVAL_KEYS == {"breakout": "bo_claude_eval_enabled"}

    def test_asking_for_bounce_is_refused_before_anything_is_written(self, monkeypatch):
        from backend.src.services.cluster import remote_control as rc
        writes: list = []
        monkeypatch.setattr(rc._risk, "update", lambda f: writes.append(f))

        with pytest.raises(rc.RemoteControlFailed):
            asyncio.run(rc.set_ai_eval("bounce", True))

        assert writes == []


# ── a peer on an older build that still says "bounce" ────────────────────────

class TestAnOlderPeerThatStillSaysBounce:

    @pytest.mark.parametrize("action", ["start", "stop", "run_now", "set_ai_eval"])
    def test_engine_control_is_refused_and_touches_nothing(
            self, node, settings_writes, action):
        ws = _Ws()

        asyncio.run(node._handle_engine_control(
            ws, {"engine": "bounce", "action": action, "enabled": True}))

        assert len(ws.sent) == 1, "the peer's button waits on this ack"
        ack = ws.sent[0]
        assert ack["type"] == MSG_ENGINE_CONTROL_ACK
        assert "unknown engine" in ack["error"]
        assert ack["is_running"] is False
        assert settings_writes == []
        assert all(e.calls == [] for e in node.fakes.values())

    def test_the_control_still_works_for_a_real_engine(self, node, settings_writes):
        # Negative control: a handler that refused everything would pass the
        # test above.
        ws = _Ws()

        asyncio.run(node._handle_engine_control(
            ws, {"engine": "breakout", "action": "set_ai_eval", "enabled": True}))
        asyncio.run(node._handle_engine_control(
            ws, {"engine": "breakout", "action": "start"}))

        assert settings_writes == [{"bo_claude_eval_enabled": 1}]
        assert node.fakes["breakout"].calls == ["start"]
        assert all(a.get("error") is None for a in ws.sent)

    def test_a_stood_down_list_naming_bounce_resumes_the_rest(self, node, monkeypatch):
        """`sync_stood_down_engines` is persisted in the shared data directory,
        and a build that still had the slot may have written "bounce" into it.
        RESUME must step over the name, not raise on it and leave every engine
        stopped."""
        db = {"stood": ["bounce", "breakout"], "trader": "local"}
        monkeypatch.setattr(ss.db_module, "get_stood_down_engines",
                            lambda: list(db["stood"]))
        monkeypatch.setattr(ss.db_module, "set_stood_down_engines",
                            lambda v: db.__setitem__("stood", list(v)))
        monkeypatch.setattr(ss.db_module, "set_active_trader",
                            lambda v, *a, **k: db.__setitem__("trader", v))
        ws = _Ws()

        asyncio.run(node._handle_resume(ws))

        assert node.fakes["breakout"].calls == ["start"]
        assert ws.sent[0]["restarted_engines"] == ["breakout"]
        assert db["stood"] == []

