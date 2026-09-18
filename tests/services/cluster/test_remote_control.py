"""A control has to land on the node that is actually trading.

When the VPS is the active trader this machine's sub-engines are stood down, so
pressing Stop here stops something that was not running while the VPS's copy
keeps generating — and the screen says "stopped". The sync server's own
`_handle_engine_control` names the failure: those buttons *"would otherwise act
on the Mac's own stood-down engine instance, which does nothing useful while
looking like it worked."*

Three properties, and each has cost a real evening:

  * **Where it lands.** Not "am I in Remote mode": under centralized signal
    generation the engines moved HERE, so the local ones are the live ones even
    though the VPS trades. This module defers that call to
    `remote_stats_facade` rather than re-deriving it.
  * **What the panel shows.** In Remote mode the settings the engines obey are
    the peer's, overlaid on the local row — overlaid, not replacing it, because
    the snapshot carries a handful of keys and a wholesale swap would blank
    every setting the peer never mentions.
  * **Where "current" is read before a toggle inverts it.** From the local row
    while writing to the peer, a toggle recomputes the same current on every
    click and re-sends the same target state for ever. Confirmed live: Bounce
    stuck OFF, Breakout stuck ON.

Nothing here reaches a socket, a broker or a database. The peer is a recorder
and the engines are recorders.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import remote_control as rc
from backend.src.services.engines import registry as engine_registry


class _Engine:
    def __init__(self, running=False):
        self.is_running = running
        self.calls: list[str] = []

    def start(self):
        self.calls.append("start")
        self.is_running = True

    def stop(self):
        self.calls.append("stop")
        self.is_running = False


class _Svc:
    def __init__(self, engine):
        self._engine = engine

    def get_instance(self):
        return self._engine


class _Peer:
    def __init__(self):
        self.remote_settings: dict = {}
        self.sent: list[tuple] = []
        self.ack: dict = {"is_running": True}
        self.raises: Exception | None = None

    async def send_engine_control(self, engine, action, **kwargs):
        self.sent.append((engine, action, kwargs))
        if self.raises:
            raise self.raises
        return self.ack


@pytest.fixture
def node(monkeypatch):
    """One machine, with the mode switchable and every side effect recorded."""
    state = {
        "remote_active": False,
        "centralized": False,
        "local_settings": {"bo_claude_eval_enabled": 1, "vol_target_sizing_enabled": 0},
        "writes": [],
    }
    breakout, reversal = _Engine(), _Engine()
    peer = _Peer()

    monkeypatch.setattr(rc._facade, "_is_remote_active",
                        lambda: state["remote_active"])
    monkeypatch.setattr(rc._facade, "_is_centralized_remote_mode",
                        lambda: state["centralized"])
    monkeypatch.setattr(rc._client, "get_instance", lambda: peer)
    monkeypatch.setattr(rc._risk, "get", lambda: dict(state["local_settings"]))
    monkeypatch.setattr(rc._risk, "update",
                        lambda f: (state["writes"].append(f),
                                   state["local_settings"].update(f)))
    monkeypatch.setattr(engine_registry, "_ENGINE_SERVICES", {
        "breakout": _Svc(breakout), "bounce": None, "reversal": _Svc(reversal),
    })
    state.update(peer=peer, breakout=breakout, reversal=reversal)
    return state


# ── Where a control lands ────────────────────────────────────────────────────

class TestWhereItLands:
    def test_locally_when_this_node_trades(self, node):
        assert rc.where() == "local"

    def test_at_the_peer_when_the_peer_trades(self, node):
        node["remote_active"] = True

        assert rc.where() == "remote"

    def test_locally_under_centralized_generation(self, node):
        """The case an operator would misread. The VPS is trading, so the
        header says REMOTE — and these engines are still the live ones,
        because generation moved here."""
        node["centralized"] = True

        assert rc.where() == "centralized"

    def test_centralized_beats_remote(self, node):
        """Both facade answers are true on that node. Reporting "remote" would
        send every control to a VPS whose engines stopped analysing."""
        node["centralized"] = True
        node["remote_active"] = True

        assert rc.where() == "centralized"


# ── Starting and stopping ────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestStartingAnEngine:
    async def test_locally_it_starts_the_local_instance(self, node):
        result = await rc.set_engine_running("breakout", True)

        assert node["breakout"].calls == ["start"]
        assert node["peer"].sent == []
        assert result["where"] == "local"
        assert result["running"] is True

    async def test_in_remote_mode_it_goes_to_the_peer_instead(self, node):
        """The whole point. Starting the local copy here generates nothing and
        says it is running."""
        node["remote_active"] = True

        result = await rc.set_engine_running("breakout", True)

        assert node["peer"].sent == [("breakout", "start", {})]
        assert node["breakout"].calls == [], "the stood-down local copy must not be touched"
        assert result["where"] == "remote"

    async def test_stopping_in_remote_mode_also_goes_to_the_peer(self, node):
        node["remote_active"] = True

        await rc.set_engine_running("reversal", False)

        assert node["peer"].sent == [("reversal", "stop", {})]
        assert node["reversal"].calls == []

    async def test_the_peers_own_answer_is_what_is_reported(self, node):
        """Not the value that was asked for. The peer is the one that knows."""
        node["remote_active"] = True
        node["peer"].ack = {"is_running": False}

        result = await rc.set_engine_running("breakout", True)

        assert result["running"] is False

    async def test_an_engine_this_install_never_built_is_not_started(self, node):
        result = await rc.set_engine_running("bounce", True)

        assert result["built"] is False
        assert result["running"] is False


@pytest.mark.asyncio
class TestWhenThePeerWillNotPlay:
    async def test_an_unreachable_peer_is_a_refusal_not_a_silent_no_op(self, node):
        node["remote_active"] = True
        node["peer"].raises = TimeoutError("no route to host")

        with pytest.raises(rc.RemoteControlFailed) as exc:
            await rc.set_engine_running("breakout", True)

        assert "could not be reached" in str(exc.value)
        assert node["breakout"].calls == [], "and it must not fall back to local"

    async def test_a_peer_that_refuses_says_why(self, node):
        node["remote_active"] = True
        node["peer"].ack = {"error": "engine not built on this node"}

        with pytest.raises(rc.RemoteControlFailed) as exc:
            await rc.set_engine_running("breakout", True)

        assert "engine not built on this node" in str(exc.value)


# ── The toggle that used to stick ────────────────────────────────────────────

@pytest.mark.asyncio
class TestTheAiEvalToggle:
    async def test_locally_it_inverts_the_local_row(self, node):
        result = await rc.set_ai_eval("breakout")

        assert node["writes"] == [{"bo_claude_eval_enabled": 0}]
        assert result["enabled"] is False
        assert result["where"] == "local"

    async def test_in_remote_mode_it_writes_nothing_locally(self, node):
        node["remote_active"] = True
        node["peer"].remote_settings = {"bo_claude_eval_enabled": 1}

        await rc.set_ai_eval("breakout")

        assert node["writes"] == []
        assert node["peer"].sent == [
            ("breakout", "set_ai_eval", {"enabled": False})]

    async def test_it_inverts_the_PEERS_value_not_this_nodes(self, node):
        """The bug. The local row says 1 and the peer says 0, so the correct
        next value is 1 — reading the local row would send 0 again."""
        node["remote_active"] = True
        node["local_settings"]["bo_claude_eval_enabled"] = 1
        node["peer"].remote_settings = {"bo_claude_eval_enabled": 0}

        result = await rc.set_ai_eval("breakout")

        assert result["enabled"] is True
        assert node["peer"].sent == [
            ("breakout", "set_ai_eval", {"enabled": True})]

    async def test_the_ack_is_recorded_so_the_next_click_sees_it(self, node):
        """Without this the snapshot never moves, the same "current" is
        recomputed for ever and the toggle is one-directional. Confirmed live:
        Bounce stuck OFF, Breakout stuck ON."""
        node["remote_active"] = True
        node["peer"].remote_settings = {"bo_claude_eval_enabled": 1}

        await rc.set_ai_eval("breakout")
        assert node["peer"].remote_settings["bo_claude_eval_enabled"] == 0

        await rc.set_ai_eval("breakout")
        assert node["peer"].sent[-1] == (
            "breakout", "set_ai_eval", {"enabled": True})

    async def test_a_refused_toggle_does_not_record_anything(self, node):
        """A snapshot updated for a write the peer rejected is a screen that
        disagrees with the node it is describing."""
        node["remote_active"] = True
        node["peer"].remote_settings = {"bo_claude_eval_enabled": 1}
        node["peer"].ack = {"error": "nope"}

        with pytest.raises(rc.RemoteControlFailed):
            await rc.set_ai_eval("breakout")

        assert node["peer"].remote_settings["bo_claude_eval_enabled"] == 1

    async def test_an_explicit_value_is_used_as_given(self, node):
        """Not every caller is a toggle."""
        await rc.set_ai_eval("breakout", enabled=True)

        assert node["writes"] == [{"bo_claude_eval_enabled": 1}]

    async def test_an_engine_with_no_such_setting_is_refused(self, node):
        """The protocol maps exactly two engines. Reversal is not one, and a
        silent no-op would read as a broken switch."""
        with pytest.raises(rc.RemoteControlFailed) as exc:
            await rc.set_ai_eval("reversal")

        assert "reversal" in str(exc.value)
        assert node["writes"] == []


# ── What the panel is shown ──────────────────────────────────────────────────

class TestTheSettingsThePanelShows:
    def test_locally_they_are_this_nodes(self, node):
        assert rc.effective_settings({"a": 1}) == {"a": 1}

    def test_in_remote_mode_the_peers_value_wins(self, node):
        node["remote_active"] = True
        node["peer"].remote_settings = {"a": 9}

        assert rc.effective_settings({"a": 1})["a"] == 9

    def test_a_setting_the_peer_never_mentions_is_kept(self, node):
        """The snapshot carries a handful of keys, not the row. Replacing
        wholesale would blank every setting the broadcast does not send."""
        node["remote_active"] = True
        node["peer"].remote_settings = {"a": 9}

        assert rc.effective_settings({"a": 1, "b": 2})["b"] == 2

    def test_the_caller_s_dict_is_not_mutated(self, node):
        """It is the freshly-read risk row; a caller that reused it would find
        the peer's values in its own settings."""
        local = {"a": 1}
        node["remote_active"] = True
        node["peer"].remote_settings = {"a": 9}

        rc.effective_settings(local)

        assert local == {"a": 1}

    def test_no_snapshot_at_all_falls_back_to_local(self, node, monkeypatch):
        """A link that has never connected has nothing to overlay. Showing an
        empty panel there would read as "every setting is off"."""
        node["remote_active"] = True

        def _boom():
            raise RuntimeError("no client")

        monkeypatch.setattr(rc._client, "get_instance", _boom)

        assert rc.effective_settings({"a": 1}) == {"a": 1}

