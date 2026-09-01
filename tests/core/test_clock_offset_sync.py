"""The Mac tells the VPS what time it is where the user is.

Owner decision, 2026-09-01: the trading clock is the user's own local time.
On the user's own machine that is simply the machine's clock. On a **VPS it is
not** — the server is in a data centre and the user is not.

An offset can be configured for exactly that case, but a fixed number does not
follow daylight saving: set the VPS to +60 for British Summer Time and it stays
+60 in November, an hour wrong for five months with nothing reporting it.

So the Mac reports its own current offset, and the VPS adopts it. Twice a year
the Mac's clocks change, its next message carries the new offset, and the VPS
follows.

Direction matters and is not symmetric. **The client is the Mac and the server
is the VPS** — the server's own module docstring calls the peer "the Mac". So
the server adopts what the client reports, and never the reverse: the machine
where the user actually is is the authority on what time it is there.

The offset is reported on the handshake (so a reconnect corrects it
immediately) and on every liveness ping (so a link that stays up for months
still follows a clock change).
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.sync import _server_peer_data as spd
from backend.src.services.cluster.sync import server as ss
from backend.src.utils.trading_clock import SETTING_KEY

pytestmark = pytest.mark.asyncio


class _Node(spd.ServerPeerDataMixin):
    def __init__(self):
        self._main_engine = None


@pytest.fixture
def node():
    return _Node()


@pytest.fixture
def settings(monkeypatch):
    """Capture what the VPS would write, and what it currently believes."""
    state = {"stored": {}, "writes": []}

    def _update(updates, **kw):
        state["writes"].append((updates, kw))
        state["stored"].update(updates)
        return state["stored"]

    monkeypatch.setattr(spd.db_module, "get_risk_settings",
                        lambda: dict(state["stored"]))
    monkeypatch.setattr(spd.db_module, "update_risk_settings", _update)
    return state


class TestTheServerAdoptsThePeersOffset:

    async def test_a_reported_offset_is_stored(self, node, settings):
        node._apply_peer_clock_offset({"clock_offset_min": 60})

        assert settings["stored"][SETTING_KEY] == 60

    async def test_it_is_marked_as_coming_from_sync(self, node, settings):
        """Otherwise applying it forwards it straight back down the channel."""
        node._apply_peer_clock_offset({"clock_offset_min": 60})

        assert settings["writes"][0][1].get("_from_sync") is True

    async def test_a_changed_offset_is_followed(self, node, settings):
        """The daylight-saving case: the Mac's clocks go back, its next message
        carries the new offset, the VPS follows."""
        node._apply_peer_clock_offset({"clock_offset_min": 60})
        node._apply_peer_clock_offset({"clock_offset_min": 0})

        assert settings["stored"][SETTING_KEY] == 0

    async def test_an_unchanged_offset_writes_nothing(self, node, settings):
        """This runs on every liveness ping. Writing each time would be a
        database write every few seconds for a value that changes twice a
        year."""
        node._apply_peer_clock_offset({"clock_offset_min": 60})
        node._apply_peer_clock_offset({"clock_offset_min": 60})
        node._apply_peer_clock_offset({"clock_offset_min": 60})

        assert len(settings["writes"]) == 1

    async def test_zero_is_a_real_offset(self, node, settings):
        """UTC+0 is a legitimate report and must not be read as absence."""
        node._apply_peer_clock_offset({"clock_offset_min": 0})

        assert settings["stored"][SETTING_KEY] == 0

    async def test_a_negative_offset_works(self, node, settings):
        node._apply_peer_clock_offset({"clock_offset_min": -300})

        assert settings["stored"][SETTING_KEY] == -300


class TestItIgnoresAnythingItCannotUse:
    """The value arrives from the other node, so it is checked like any other
    thing a peer sends."""

    async def test_a_message_without_an_offset_changes_nothing(self, node,
                                                               settings):
        node._apply_peer_clock_offset({"type": "ping"})

        assert settings["writes"] == []

    @pytest.mark.parametrize("bad", ["abc", None, 99999, -99999, [], {}])
    async def test_nonsense_is_ignored_rather_than_stored(self, node, settings,
                                                          bad):
        node._apply_peer_clock_offset({"clock_offset_min": bad})

        assert settings["writes"] == []

    async def test_a_database_failure_does_not_break_the_message(self, node,
                                                                 monkeypatch):
        """This runs inside the connection handler and on every ping. Raising
        would drop the link over a clock detail."""
        def _boom():
            raise RuntimeError("database is locked")
        monkeypatch.setattr(spd.db_module, "get_risk_settings", _boom)

        node._apply_peer_clock_offset({"clock_offset_min": 60})  # must not raise


class TestTheOffsetIsNotAnOrDINARYSyncedSetting:
    """If it were in _SYNCED_SETTINGS_KEYS the settings broadcast would push
    the VPS's offset back to the Mac, and the Mac would start running on its
    server's clock. The whole point is that it travels one way."""

    async def test_it_is_absent_from_the_synced_keys(self):
        assert SETTING_KEY not in ss._SYNCED_SETTINGS_KEYS

    async def test_a_peer_cannot_set_it_through_a_settings_proposal(self,
                                                                    monkeypatch):
        """The other route into the same column. It has to be closed too, or
        the one-way rule holds only by convention."""
        applied = []
        monkeypatch.setattr(ss.db_module, "update_risk_settings",
                            lambda u, **kw: applied.append(u))

        srv = ss.SyncServer.__new__(ss.SyncServer)
        srv._clients = set()

        class _Ws:
            async def send(self, raw):
                pass

        async def _noop(*_a, **_kw):
            return None
        srv._broadcast = _noop
        monkeypatch.setattr(ss.SyncServer, "_settings_snapshot", lambda _s: {})

        await srv._handle_settings_propose(_Ws(), {"updates": {SETTING_KEY: 480}})

        assert applied == [], "a peer set the trading clock through a proposal"


class TestTheMacActuallyReportsIt:
    """The server half is useless if the client never sends the value."""

    def test_the_handshake_carries_it(self):
        import ast
        import pathlib

        from backend.src.services.cluster.sync import client as sc

        src = pathlib.Path(sc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_connect_once")
        hello = [c for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "make"
                 and c.args and getattr(c.args[0], "id", "") == "MSG_HELLO"]

        assert hello, "no MSG_HELLO is built in _connect_once"
        assert any(k.arg == "clock_offset_min" for k in hello[0].keywords), (
            "the handshake does not report this machine's offset, so a "
            "reconnect cannot correct the VPS's clock"
        )

    def test_every_ping_carries_it(self):
        """The handshake alone is not enough: a link can stay up for months,
        and clocks change twice a year."""
        import ast
        import pathlib

        from backend.src.services.cluster.sync import client as sc

        src = pathlib.Path(sc.__file__).read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                  and n.name == "_liveness_ping_loop")
        pings = [c for c in ast.walk(fn)
                 if isinstance(c, ast.Call) and isinstance(c.func, ast.Name)
                 and c.func.id == "make"
                 and c.args and getattr(c.args[0], "id", "") == "MSG_PING"]

        assert pings and any(k.arg == "clock_offset_min" for k in pings[0].keywords)

    def test_the_reported_value_is_a_definite_number(self):
        """`offset_minutes()` returns None for "use the machine's own clock",
        which is the right answer locally and useless to tell someone else."""
        from backend.src.services.risk import clock as risk_clock

        assert isinstance(risk_clock.effective_offset_minutes(), int)

    def test_it_reports_the_configured_offset_when_there_is_one(self,
                                                                monkeypatch):
        """A Mac with an explicit offset reports THAT, not its machine's — so
        whatever clock the Mac keeps, the VPS matches it."""
        from backend.src.services.risk import clock as risk_clock
        monkeypatch.setattr(risk_clock, "offset_minutes", lambda: 345)

        assert risk_clock.effective_offset_minutes() == 345


class TestTheServerIsWiredToCallIt:
    def test_the_handshake_applies_it(self):
        import pathlib

        src = pathlib.Path(ss.__file__).read_text(encoding="utf-8")
        handshake = src[src.index("async def _handle_connection"):
                        src.index("async def _dispatch")]

        assert "_apply_peer_clock_offset(msg)" in handshake

    def test_the_ping_handler_applies_it(self):
        import pathlib

        src = pathlib.Path(ss.__file__).read_text(encoding="utf-8")
        dispatch = src[src.index("async def _dispatch"):
                       src.index("async def _handle_engine_control")]

        assert "_apply_peer_clock_offset(msg)" in dispatch
