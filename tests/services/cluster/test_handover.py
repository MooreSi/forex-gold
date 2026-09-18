"""Exactly one node may execute new trades. This is the file that holds it.

Both nodes are pointed at the same MT5 account. Two active nodes is two sets of
engines opening positions against one balance, and nothing on either screen
says so — it shows up as trades nobody placed.

**The ordering is the behaviour.** Every test here is really about order:

  * taking over asks the peer to stand down and waits for the acknowledgement
    BEFORE this node is marked active or its engines are started;
  * handing back stops this node's engines BEFORE asking the peer to resume.

Each half is arranged so that a failure leaves the account with **no** active
trader rather than two. That is a deliberate trade: "nothing is trading" is a
state the operator can see and retry from, and "both are trading" is not.

Nothing here touches a broker, a socket or a database. The peer is a recorder,
the engines are recorders, and `set_active_trader` writes into a dict. What is
being tested is a sequence of decisions, which is exactly what a test can own —
and what a demo session cannot check cheaply, because reproducing a peer that
fails to acknowledge means breaking a live link on purpose.

**This code has not been through a demo session.** Its tests are green and that
is not sign-off: see docs/todo/frontend/react-port/PROGRESS.md.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster import handover
from backend.src.services.engines import registry as engine_registry

# Scoped to the two async classes rather than the module: the last two tests
# are synchronous, and a module-wide mark makes pytest warn on each of them.
pytestmark: list = []


class _Engine:
    def __init__(self, running: bool):
        self.is_running = running
        self.events: list[str] = []

    def start(self):
        self.events.append("start")
        self.is_running = True

    def stop(self):
        self.events.append("stop")
        self.is_running = False


class _Peer:
    """The paired node, as this side can see it.

    Records the order of everything, because order is the claim. `raises` makes
    it the node that does not answer — a VPS mid-reboot, or a link that dropped
    between the check and the request.
    """

    def __init__(self, order, conn_state="connected", ack=None, raises=None):
        self.conn_state = conn_state
        self.ack = ack if ack is not None else {"open_positions": []}
        self.raises = raises
        self.calls: list[str] = []
        # The SHARED order list, not a private one. A peer with its own list
        # can say "stand_down happened" and "the flag was set" but not which
        # came first — and which came first is the entire safety property.
        self._order = order

    async def request_stand_down(self, timeout=15.0):
        self.calls.append("stand_down")
        self._order.append("peer:stand_down")
        if self.raises:
            raise self.raises
        return self.ack

    async def request_resume(self, timeout=15.0):
        self.calls.append("resume")
        self._order.append("peer:resume")
        if self.raises:
            raise self.raises


@pytest.fixture
def cluster(monkeypatch):
    """A peer, two engines and the stored active_trader, all in memory.

    The engine start/stop and the `set_active_trader` write append to ONE
    shared list. Two lists would record that both things happened and lose the
    only fact this file is about, which is which happened first.
    """
    state: dict = {"trader": "remote_vps", "order": []}
    breakout = _Engine(running=False)
    reversal = _Engine(running=False)
    peer = _Peer(state["order"])

    class _Svc:
        def __init__(self, engine, name):
            self._engine, self._name = engine, name

        def get_instance(self):
            return self

        # The engine's own surface, with every lifecycle call timestamped into
        # the shared order list.
        @property
        def is_running(self):
            return self._engine.is_running

        def start(self):
            state["order"].append(f"start:{self._name}")
            self._engine.start()

        def stop(self):
            state["order"].append(f"stop:{self._name}")
            self._engine.stop()

    # The registry, not this module: the engine table and its bulk loops live
    # in services/engines/registry.py, so that the controller and the handover
    # cannot drift apart about which engines exist.
    monkeypatch.setattr(engine_registry, "_ENGINE_SERVICES", {
        "breakout": _Svc(breakout, "breakout"),
        "bounce": None,
        "reversal": _Svc(reversal, "reversal"),
    })
    monkeypatch.setattr(handover._client, "get_instance", lambda: peer)

    def _set(value, *a, **k):
        state["order"].append(f"trader:{value}")
        state["trader"] = value

    monkeypatch.setattr(handover._node, "set_active_trader", _set)

    state.update(peer=peer, breakout=breakout, reversal=reversal)
    return state


# ── Taking over ──────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestTakingOver:
    async def test_the_peer_stands_down_before_this_node_starts_trading(self, cluster):
        """The one that matters. Marking this node active first means two
        nodes believing they own the same account."""
        await handover.take_over_locally()

        assert cluster["order"] == [
            "peer:stand_down", "trader:local", "start:breakout", "start:reversal",
        ]

    async def test_the_engines_are_started(self, cluster):
        await handover.take_over_locally()

        assert cluster["breakout"].is_running is True
        assert cluster["reversal"].is_running is True

    async def test_a_peer_that_does_not_answer_leaves_this_node_view_only(
        self, cluster,
    ):
        """Fail closed. The peer may still be trading, so this node must not
        start — and it must not be recorded as the active trader either."""
        cluster["peer"].raises = TimeoutError("no ack in 15s")

        with pytest.raises(handover.HandoverRefused) as exc:
            await handover.take_over_locally()

        assert cluster["trader"] == "remote_vps"
        assert cluster["order"] == ["peer:stand_down"], (
            "nothing may follow a stand-down the peer did not acknowledge")
        assert cluster["breakout"].is_running is False
        assert "still the active trader" in str(exc.value)

    async def test_it_says_how_many_of_the_peers_positions_keep_running(
        self, cluster,
    ):
        """Standing down stops new entries; it does not close anything. An
        operator told "the VPS stood down" will assume flat unless told."""
        cluster["peer"].ack = {"open_positions": [{"ticket": 1}, {"ticket": 2}]}

        result = await handover.take_over_locally()

        assert result["remote_open_positions"] == 2
        assert "2 of its position(s)" in result["note"]

    async def test_with_no_link_it_refuses_without_asking_anything(self, cluster):
        cluster["peer"].conn_state = "disconnected"

        with pytest.raises(handover.HandoverRefused) as exc:
            await handover.take_over_locally()

        assert cluster["peer"].calls == []
        assert cluster["order"] == []
        assert "Pair one" in str(exc.value)


# ── Handing back ─────────────────────────────────────────────────────────────

@pytest.mark.asyncio
class TestHandingBack:
    async def test_the_engines_stop_before_the_peer_is_asked_to_resume(self, cluster):
        """The mirror image. Asking the peer first would have both running
        until this node got round to stopping."""
        cluster["breakout"].is_running = True
        cluster["reversal"].is_running = True

        await handover.hand_back_to_remote()

        assert cluster["order"] == [
            "stop:breakout", "stop:reversal", "peer:resume", "trader:remote_vps",
        ]

    async def test_a_peer_that_does_not_answer_leaves_nothing_trading(self, cluster):
        """Engines are already stopped by then, and they stay stopped: whether
        the peer resumed is unknown, and guessing it did is how both end up
        running."""
        cluster["breakout"].is_running = True
        cluster["peer"].raises = TimeoutError("no ack in 15s")

        with pytest.raises(handover.HandoverRefused) as exc:
            await handover.hand_back_to_remote()

        assert cluster["breakout"].is_running is False
        assert cluster["trader"] == "remote_vps"
        assert "view-only" in str(exc.value)
        assert "may" in str(exc.value), "it must not claim the peer resumed"

    async def test_it_says_how_many_local_positions_keep_running(self, cluster):
        """Handing back closes nothing — the monitor loop keeps managing what
        is already open. "View-only" does not mean "flat"."""
        result = await handover.hand_back_to_remote(
            open_trades=[{"ticket": 1}, {"ticket": 2}, {"ticket": 3}])

        assert result["local_open_positions"] == 3
        assert "3 local position(s)" in result["note"]

    async def test_with_no_link_it_refuses_without_stopping_anything(self, cluster):
        """A disconnected peer is not a reason to stop trading locally. This
        node is the active trader and stays it."""
        cluster["breakout"].is_running = True
        cluster["peer"].conn_state = "disconnected"

        with pytest.raises(handover.HandoverRefused):
            await handover.hand_back_to_remote()

        assert cluster["breakout"].is_running is True
        assert cluster["peer"].calls == []


# ── The empty slot ───────────────────────────────────────────────────────────

def test_the_bounce_slot_is_skipped_rather_than_crashing():
    """Bounce's code went on 2026-09-14 and its NAME stayed, so a paired node
    on the older build does not see Reversal shift into its position. A None
    service must be stepped over, not called."""
    assert engine_registry._ENGINE_SERVICES["bounce"] is None
    assert engine_registry._of(None) is None


def test_the_binding_order_is_fixed():
    """`server_start` binds them positionally. A reorder here silently sends a
    paired node the wrong engine under the right name."""
    assert engine_registry.ENGINE_NAMES == ("breakout", "bounce", "reversal")


def test_bounce_is_never_bulk_started():
    """A structural claim, and honestly a weak one.

    Deleting the `_NOT_BULK_STARTED` guard from `registry.start_stopped` leaves
    every test in this file green — the planted mutation survived on
    2026-09-18. That is not a gap in the tests: the slot holds None, `_instance`
    returns None for it, and the loop skips it anyway. The guard is belt and
    braces, exactly as the comment beside it says, and it is asserted here so
    that the safety property is recorded rather than incidental — the day
    something is put back in that slot, the guard is what stops it being
    started by a mode switch.
    """
    assert "bounce" in engine_registry._NOT_BULK_STARTED
