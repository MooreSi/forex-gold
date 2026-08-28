"""The cluster's own data layer: node identity, the consolidated ledger, and
the three-way switch that decides which node analyses anything.

Two of these get one thing badly wrong if they are wrong at all:

  * should_generate_signals_here() answering True on both nodes means both
    generate the same signals; answering False on both means nothing generates
    at all and no trade is ever taken. It is an AND of three conditions and
    every one of them has to be checked.
  * record_consolidated_trade() is called twice for the same trade by design --
    once at close, and again 30+ minutes later when max_tp_hit is finally
    known. The second push is partial. Its ON CONFLICT uses COALESCE so that
    partial push cannot clobber rr or max_tp_hit back to NULL.

Runs against a real sqlite database via fresh_db. The SQL -- the upsert, the
COALESCE, the uniqueness -- is the behaviour, so faking it would test nothing.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.cluster import sync_repo as repo


pytestmark = pytest.mark.usefixtures("fresh_db")


def _trade(**over):
    t = {"trade_id": "T1", "engine": "main", "direction": "BUY",
         "strategy": "scalp", "open_time": 1000.0, "close_time": 2000.0,
         "pnl_dollars": 12.5, "outcome": "win", "tg_source": "Gold Diggers VIP",
         "mt5_ticket": "188325", "max_tp_hit": None, "rr": None}
    t.update(over)
    return t


class TestNodeIdentity:
    def test_it_creates_one_on_first_call(self):
        assert len(repo.get_or_create_node_id()) == 12

    def test_IT_IS_STABLE(self):
        """It tags every row this node pushes into the other side's ledger.
        A changing id makes one node look like many."""
        assert repo.get_or_create_node_id() == repo.get_or_create_node_id()

    def test_the_active_trader_DEFAULTS_TO_THE_VPS(self):
        """The VPS is the always-on trader unless this Mac has explicitly
        taken over. Defaulting to local would have a fresh Mac believe it is
        in charge while the VPS is also trading."""
        assert repo.get_active_trader() == "remote_vps"

    def test_it_round_trips(self):
        repo.set_active_trader("local")
        assert repo.get_active_trader() == "local"

    def test_is_remote_node_is_the_STRING_one(self):
        """app_config stores text. An int 1 is not this value, and a VPS that
        answered False here would run the analytical generators it is meant
        to leave to the Mac."""
        from backend.src.db import database as db_module
        assert repo.is_remote_node() is False

        db_module.set_app_config("sync_server_enabled", "1")
        assert repo.is_remote_node() is True


class TestShouldGenerateSignalsHere:
    """False only when centralization is ON, this node is physically the VPS,
    AND the VPS is the active trader. Anything else generates."""

    @pytest.fixture(autouse=True)
    def wiring(self, monkeypatch):
        from backend.src.db import database as db_module
        from backend.src.services.cluster.sync import server as sync_server
        state = {"rs": {"centralized_signal_gen_enabled": 0}, "server": None}
        monkeypatch.setattr(repo, "get_risk_settings", lambda: state["rs"])
        monkeypatch.setattr(sync_server, "get_instance", lambda: state["server"])
        return state

    def test_centralization_off_generates(self, wiring):
        assert repo.should_generate_signals_here() is True

    def test_the_MAC_generates_even_with_centralization_on(self, wiring):
        """No SyncServer means this is the Mac, and centralization moves
        generation TO the Mac."""
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        wiring["server"] = None
        assert repo.should_generate_signals_here() is True

    def test_the_VPS_stops_generating(self, wiring):
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        wiring["server"] = object()
        repo.set_active_trader("remote_vps")
        assert repo.should_generate_signals_here() is False

    def test_a_STOOD_DOWN_vps_generates_again(self, wiring):
        """Local mode: the Mac is trading, so the VPS's centralization is
        moot and it is back to normal behaviour."""
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        wiring["server"] = object()
        repo.set_active_trader("local")
        assert repo.should_generate_signals_here() is True

    def test_EXACTLY_ONE_NODE_generates_under_centralization(self, wiring):
        """The property, rather than the four branches. Both True is double
        signals; both False is no trades at all."""
        wiring["rs"] = {"centralized_signal_gen_enabled": 1}
        repo.set_active_trader("remote_vps")

        wiring["server"] = None
        mac = repo.should_generate_signals_here()
        wiring["server"] = object()
        vps = repo.should_generate_signals_here()

        assert mac != vps, f"mac={mac} vps={vps} — both nodes agreed"


class TestTheConsolidatedLedger:
    def test_a_trade_is_recorded(self):
        repo.record_consolidated_trade("node-a", _trade())
        rows = repo.get_consolidated_trades()
        assert len(rows) == 1
        assert rows[0]["trade_id"] == "T1"
        assert rows[0]["pnl_dollars"] == 12.5

    def test_RE_DELIVERY_DOES_NOT_DUPLICATE(self):
        """A reconnect replays what the other node already sent. UNIQUE
        (node_id, trade_id) plus the upsert is what makes that safe."""
        repo.record_consolidated_trade("node-a", _trade())
        repo.record_consolidated_trade("node-a", _trade())
        assert len(repo.get_consolidated_trades()) == 1

    def test_the_same_trade_id_from_a_DIFFERENT_node_is_its_own_row(self):
        """trade_id is only unique within a node. Keying on it alone would
        have one node's trade overwrite the other's."""
        repo.record_consolidated_trade("node-a", _trade())
        repo.record_consolidated_trade("node-b", _trade())
        assert len(repo.get_consolidated_trades()) == 2

    def test_a_re_push_updates_the_outcome(self):
        repo.record_consolidated_trade("node-a", _trade(outcome="win", pnl_dollars=12.5))
        repo.record_consolidated_trade("node-a", _trade(outcome="loss", pnl_dollars=-8.0))

        row = repo.get_consolidated_trades()[0]
        assert row["outcome"] == "loss"
        assert row["pnl_dollars"] == -8.0

    def test_THE_LATE_PARTIAL_PUSH_DOES_NOT_ERASE_RR(self):
        """The documented reason for the COALESCE. max_tp_hit is only known
        30+ minutes after close, so a second, partial push arrives carrying
        nothing else. Plain excluded.rr would blank the value recorded at
        close."""
        repo.record_consolidated_trade("node-a", _trade(rr=2.5, max_tp_hit=None))

        repo.record_consolidated_trade("node-a", _trade(max_tp_hit="TP4", rr=None))

        row = repo.get_consolidated_trades()[0]
        assert row["max_tp_hit"] == "TP4", "the late push did not land"
        assert row["rr"] == 2.5, "the late push erased rr"

    def test_A_REPLAYED_CLOSE_MESSAGE_DOES_NOT_ERASE_MAX_TP_HIT(self):
        """The other half of the same COALESCE, and the half the first draft
        of this file missed -- mutation survived removing the max_tp_hit
        COALESCE because every test only ever pushed None BEFORE the real
        value, never after.

        The sequence that needs it: close-time push, late push sets
        max_tp_hit, then the close-time message is delivered again on a
        reconnect still carrying None. Without COALESCE that replay blanks a
        value that took 30 minutes to establish."""
        repo.record_consolidated_trade("node-a", _trade(max_tp_hit=None))
        repo.record_consolidated_trade("node-a", _trade(max_tp_hit="TP4"))

        repo.record_consolidated_trade("node-a", _trade(max_tp_hit=None))

        assert repo.get_consolidated_trades()[0]["max_tp_hit"] == "TP4"

    def test_a_real_value_still_overwrites(self):
        """COALESCE must not make these fields write-once."""
        repo.record_consolidated_trade("node-a", _trade(rr=2.5, max_tp_hit="TP2"))
        repo.record_consolidated_trade("node-a", _trade(rr=3.5, max_tp_hit="TP5"))

        row = repo.get_consolidated_trades()[0]
        assert row["rr"] == 3.5
        assert row["max_tp_hit"] == "TP5"

    def test_the_ticket_maps_span_all_nodes(self):
        """history.py merges these in for tickets with no local row -- trades
        the OTHER node placed. Filtering to this node would leave them blank."""
        repo.record_consolidated_trade("node-a", _trade(trade_id="T1", mt5_ticket="111"))
        repo.record_consolidated_trade("node-b", _trade(trade_id="T2", mt5_ticket="222",
                                                        strategy="swing", direction="SELL"))

        sources, strategies, directions = repo.get_consolidated_ticket_maps()

        assert sources["111"] == "Gold Diggers VIP"
        assert strategies["222"] == "swing"
        assert directions["222"] == "SELL"


class TestStoodDownEngines:
    def test_it_is_empty_by_default(self):
        assert repo.get_stood_down_engines() == []

    def test_it_round_trips(self):
        repo.set_stood_down_engines(["breakout", "re"])
        assert repo.get_stood_down_engines() == ["breakout", "re"]

    def test_a_corrupt_value_reads_as_empty(self):
        """Read during RESUME. Raising there would leave the engines stopped
        with no way back short of a restart."""
        from backend.src.db import database as db_module
        db_module.set_app_config("sync_stood_down_engines", "{not json")
        assert repo.get_stood_down_engines() == []


class TestMirroringAForwardedSignal:
    def _kwargs(self, **over):
        k = {"tg_source": "Gold Diggers VIP", "direction": "BUY",
             "entry_low": 4500.0, "entry_high": 4502.0, "stop_loss": 4495.0,
             "tp1": 4505.0, "tp2": None, "tp3": None, "tp4": None,
             "tp5": None, "tp6": None, "tp7": None, "tp8": None,
             "lot_size": 0.05}
        k.update(over)
        return k

    def _rows(self):
        from backend.src.db.database import db
        with db() as conn:
            return [dict(r) for r in conn.execute("SELECT * FROM vantage_signals")]

    def test_it_inserts_the_missing_signal(self):
        """Only there so the trade's foreign key resolves -- the signal was
        created in the Mac's table, never this node's."""
        repo.mirror_insert_signal_if_absent("SIG-1", self._kwargs())

        rows = self._rows()
        assert len(rows) == 1
        assert rows[0]["signal_id"] == "SIG-1"
        assert rows[0]["direction"] == "BUY"
        assert rows[0]["stop_loss"] == 4495.0

    def test_it_is_IDEMPOTENT(self):
        """Called on every forwarded trade. A second trade against the same
        signal must not insert a duplicate."""
        repo.mirror_insert_signal_if_absent("SIG-1", self._kwargs())
        repo.mirror_insert_signal_if_absent("SIG-1", self._kwargs())
        assert len(self._rows()) == 1

    def test_it_does_not_overwrite_an_existing_signal(self):
        """"if_absent" -- a signal this node generated itself must not be
        rewritten by a forward that happens to share an id."""
        repo.mirror_insert_signal_if_absent("SIG-1", self._kwargs(direction="BUY"))
        repo.mirror_insert_signal_if_absent("SIG-1", self._kwargs(direction="SELL"))
        assert self._rows()[0]["direction"] == "BUY"

    def test_a_missing_source_gets_a_named_placeholder(self):
        """Blank would show as an unattributed trade in history."""
        repo.mirror_insert_signal_if_absent("SIG-1", self._kwargs(tg_source=None))
        assert self._rows()[0]["source_name"] == "centralized_forward"

    def test_the_mirrored_row_says_what_it_is(self):
        """It is not a signal this node analysed. Anyone reading the table
        later needs to know that."""
        repo.mirror_insert_signal_if_absent("SIG-1", self._kwargs())
        assert "Mirrored" in self._rows()[0]["notes"]
