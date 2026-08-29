"""Broker vs DB reconciliation (stage3/030), report-only.

Broker and DB are dual-written with no arbiter: `open_trade` places the real
order and THEN inserts the row, so a crash in between leaves a live position
the app does not manage. The mirror gap exists on close. Nothing scanned for
it, and 020's parked `unknown` signals have no resolver.

This is the diff engine: broker snapshot + DB snapshot in, typed differences
out. Deliberately a pure function with no I/O, because it is the part where a
mistake is expensive and a test is cheap.

Report-only is the shipped default, confirmed by Simon in
docs/simon-handover/001-trading-defaults.md ("report-only for the first week,
then switch to repair"). Nothing here writes anything, to either side.

The single most important property, tested structurally as well as
behaviourally: **reconciliation never writes to the broker.** It is an
arbiter, and an arbiter that can place or close orders is just another writer.
"""
from __future__ import annotations

import pytest

from backend.src.services.positions import reconciliation as rec


TRADE_ID = "5b88a61e-6g3f-42"


def _pos(ticket=111, comment="", volume=0.05, price=4000.0):
    return {"ticket": ticket, "comment": comment, "volume": volume,
            "open_price": price, "type": "BUY"}


def _deal(position_id=55, entry=1, comment="", price=4010.0, profit=12.5,
          order=222, time=1000):
    return {"position_id": position_id, "entry": entry, "comment": comment,
            "price": price, "profit": profit, "swap": 0.0, "fee": 0.0,
            "order": order, "ticket": order, "time": time, "volume": 0.05}


def _db(trade_id=TRADE_ID, ticket=111, status="open"):
    return {"trade_id": trade_id, "mt5_ticket": ticket, "status": status,
            "direction": "BUY", "entry_price": 4000.0, "lot_size": 0.05,
            "remaining_lots": 0.05, "strategy": "scalp"}


def _kinds(diff):
    return sorted(e.kind for e in diff.entries)


class TestMatched:
    def test_a_position_the_db_knows_about_is_matched(self):
        d = rec.diff_snapshots(broker_positions=[_pos(ticket=111)],
                               broker_deals=[], db_open_trades=[_db(ticket=111)])

        assert _kinds(d) == ["matched"]
        assert d.needs_attention is False

    def test_matching_is_by_TICKET_not_by_order(self):
        """Two open trades, tickets swapped in the lists. Positional matching
        would pair the wrong rows and report two false differences."""
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=222), _pos(ticket=111)],
            broker_deals=[],
            db_open_trades=[_db(trade_id="t-a", ticket=111),
                            _db(trade_id="t-b", ticket=222)])

        assert _kinds(d) == ["matched", "matched"]

    def test_a_ticketless_row_matches_on_its_ORDER_COMMENT(self):
        """An EA template row carries ticket 0 until a leg fill promotes it.
        The comment is the only link back, and without this every template
        placeholder would be reported as a broker-only orphan."""
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=999, comment=f"ea:{TRADE_ID[:10]}a1")],
            broker_deals=[],
            db_open_trades=[_db(ticket=0)])

        assert _kinds(d) == ["matched"]

    def test_a_bridge_order_comment_matches_too(self):
        """stage3/010 stamps py:<trade_id> on bridge sends for exactly this."""
        from backend.src.services.broker.dedup import comment_for_bridge_order

        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=999,
                                   comment=comment_for_bridge_order(TRADE_ID))],
            broker_deals=[], db_open_trades=[_db(ticket=0)])

        assert _kinds(d) == ["matched"]


class TestBrokerOnly:
    def test_a_position_the_db_does_not_know_is_reported(self):
        """The crash-between-place-and-record case: a live position nothing is
        managing. No stop watching, no target, no harvest."""
        d = rec.diff_snapshots(broker_positions=[_pos(ticket=111)],
                               broker_deals=[], db_open_trades=[])

        assert _kinds(d) == ["broker_only_manual"]
        assert d.needs_attention is True

    def test_it_carries_the_ticket_so_it_can_be_adopted(self):
        d = rec.diff_snapshots(broker_positions=[_pos(ticket=111, price=4001.5)],
                               broker_deals=[], db_open_trades=[])

        e = d.entries[0]
        assert e.ticket == 111
        assert e.entry_price == 4001.5

    def test_a_position_matching_a_CLOSED_db_trade_is_still_broker_only(self):
        """The DB thinks it is finished; the broker disagrees. Treating a
        closed row as a match would hide a live position."""
        d = rec.diff_snapshots(broker_positions=[_pos(ticket=111)],
                               broker_deals=[],
                               db_open_trades=[_db(ticket=111, status="closed")])

        assert "broker_only_manual" in _kinds(d)


class TestDbOnly:
    def test_a_db_trade_gone_from_the_broker_WITH_a_closing_deal(self):
        """It closed while the app was not looking. The deal carries the real
        exit price and profit, so this is repairable."""
        d = rec.diff_snapshots(
            broker_positions=[],
            broker_deals=[_deal(position_id=111, entry=1, price=4010.0, profit=12.5)],
            db_open_trades=[_db(ticket=111)])

        e = d.entries[0]
        assert e.kind == "db_only_closed"
        assert e.close_price == 4010.0
        assert e.profit == 12.5

    def test_a_db_trade_gone_with_NO_evidence_is_FLAGGED_not_closed(self):
        """The boundary that matters. No position and no deal is not proof it
        closed -- it is equally consistent with a broker read that failed.
        Closing it on that basis would book a fabricated outcome."""
        d = rec.diff_snapshots(broker_positions=[], broker_deals=[],
                               db_open_trades=[_db(ticket=111)])

        assert _kinds(d) == ["db_only_no_evidence"]
        assert d.needs_attention is True

    def test_partial_closing_deals_are_summed(self):
        """A scaled-out trade closes across several deals. Taking only the
        last would under-report the realised result."""
        d = rec.diff_snapshots(
            broker_positions=[],
            broker_deals=[_deal(position_id=111, entry=1, profit=5.0, time=1),
                          _deal(position_id=111, entry=1, profit=7.5, time=2)],
            db_open_trades=[_db(ticket=111)])

        assert d.entries[0].profit == 12.5

    def test_the_LAST_deal_supplies_the_close_price(self):
        d = rec.diff_snapshots(
            broker_positions=[],
            broker_deals=[_deal(position_id=111, entry=1, price=4005.0, time=1),
                          _deal(position_id=111, entry=1, price=4012.0, time=2)],
            db_open_trades=[_db(ticket=111)])

        assert d.entries[0].close_price == 4012.0

    def test_an_OPENING_deal_is_not_evidence_of_a_close(self):
        """entry == 0 is the open. Reading it as a close would record the
        trade shut at its own entry price."""
        d = rec.diff_snapshots(
            broker_positions=[],
            broker_deals=[_deal(position_id=111, entry=0, price=4000.0)],
            db_open_trades=[_db(ticket=111)])

        assert _kinds(d) == ["db_only_no_evidence"]


class TestResolvingParkedUnknownSignals:
    """020 parks a signal whose send got no answer. Only broker truth resolves
    it, and both directions are wrong in different expensive ways."""

    def test_an_unknown_signal_with_a_live_position_resolved_to_FILLED(self):
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=111, comment=f"py:{TRADE_ID[:10]}")],
            broker_deals=[], db_open_trades=[],
            unknown_signals=[{"signal_id": "sig-1", "trade_id": TRADE_ID}])

        kinds = _kinds(d)
        assert "unknown_filled" in kinds

    def test_an_unknown_signal_with_a_closing_deal_resolved_to_FILLED(self):
        """It filled AND closed while parked. Still filled -- the money moved."""
        d = rec.diff_snapshots(
            broker_positions=[], broker_deals=[
                _deal(position_id=77, entry=0, comment=f"py:{TRADE_ID[:10]}")],
            db_open_trades=[],
            unknown_signals=[{"signal_id": "sig-1", "trade_id": TRADE_ID}])

        assert "unknown_filled" in _kinds(d)

    def test_an_unknown_signal_with_NO_broker_trace_resolved_to_NOT_FILLED(self):
        """Safe to release back to pending -- but that is a repair, and this
        release is report-only."""
        d = rec.diff_snapshots(broker_positions=[], broker_deals=[],
                               db_open_trades=[],
                               unknown_signals=[{"signal_id": "sig-1",
                                                 "trade_id": TRADE_ID}])

        assert _kinds(d) == ["unknown_not_filled"]

    def test_ANOTHER_trades_position_does_not_resolve_it(self):
        """The negative control. If any position resolved any unknown signal,
        every parked signal would be declared filled and never retried."""
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=111, comment="py:ffffffffff")],
            broker_deals=[], db_open_trades=[],
            unknown_signals=[{"signal_id": "sig-1", "trade_id": TRADE_ID}])

        assert "unknown_not_filled" in _kinds(d)


class TestNoiseIsNotADifference:
    def test_broker_written_comments_do_not_match_anything(self):
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=999, comment="[sl 4046.50]")],
            broker_deals=[], db_open_trades=[_db(ticket=0)])

        assert "matched" not in _kinds(d)

    def test_an_empty_world_is_an_empty_diff(self):
        d = rec.diff_snapshots(broker_positions=[], broker_deals=[],
                               db_open_trades=[])

        assert d.entries == []
        assert d.needs_attention is False

    def test_only_OPEN_db_rows_are_considered(self):
        """Closed and cancelled rows are history, not a reconciliation
        target."""
        d = rec.diff_snapshots(
            broker_positions=[], broker_deals=[],
            db_open_trades=[_db(trade_id="t-1", ticket=1, status="closed"),
                            _db(trade_id="t-2", ticket=2, status="cancelled")])

        assert d.entries == []


class TestItIsProvablyReadOnlyAtTheBroker:
    """An arbiter that can place or close orders is just another writer."""

    def test_the_module_CALLS_no_order_writing_function(self):
        """Checked against the parsed code, not the text. A substring scan
        over the source also matches the docstring, which explains WHY these
        must not appear -- so it would fail on an accurate comment and pass on
        an obfuscated call. The AST only sees what actually executes."""
        import ast
        import inspect

        tree = ast.parse(inspect.getsource(rec))
        called = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                fn = node.func
                if isinstance(fn, ast.Name):
                    called.add(fn.id)
                elif isinstance(fn, ast.Attribute):
                    called.add(fn.attr)

        forbidden = {"place_order", "close_position", "modify_order",
                     "order_send", "open_trade", "partial_close_trade",
                     "record_close", "close_trade"}
        assert not (called & forbidden), (
            f"reconciliation calls {called & forbidden} — it must be read-only "
            "at the broker")

    def test_the_diff_engine_takes_no_bridge_at_all(self):
        """It cannot call the broker because it is never handed one. Snapshots
        in, differences out."""
        import inspect
        params = inspect.signature(rec.diff_snapshots).parameters

        assert "bridge" not in params
        for name in params:
            assert "bridge" not in name.lower()

    def test_it_writes_nothing_in_report_only_mode(self, fresh_db):
        """The shipped default, per Simon's answer in 001-trading-defaults."""
        from backend.src.db.database import db

        d = rec.diff_snapshots(broker_positions=[_pos(ticket=111)],
                               broker_deals=[], db_open_trades=[])
        rec.report(d)

        with db() as conn:
            n = conn.execute(
                "SELECT COUNT(*) FROM vantage_simulated_trades").fetchone()[0]
        assert n == 0, "report-only reconciliation wrote to the database"


class TestTheReport:
    def test_it_summarises_every_kind(self):
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=111), _pos(ticket=222)],
            broker_deals=[],
            db_open_trades=[_db(trade_id="t-b", ticket=222),
                            _db(trade_id="t-c", ticket=333)])

        text = rec.report(d)

        assert "broker_only_manual" in text
        assert "db_only_no_evidence" in text

    def test_a_clean_reconciliation_says_so(self):
        text = rec.report(rec.diff_snapshots([], [], []))

        assert "no differences" in text.lower()


# ── the periodic pass ────────────────────────────────────────────────────────

class _RecBridge:
    def __init__(self, positions=None, deals=None, error=None, configured=True):
        self._positions = positions
        self._deals = deals if deals is not None else []
        self._error = error
        self._configured = configured

    def is_configured(self):
        return self._configured

    async def get_positions(self):
        if self._error:
            raise self._error
        return self._positions

    async def get_deal_history(self, days=7):
        return self._deals


@pytest.mark.asyncio
class TestThePeriodicPass:
    async def test_it_reports_a_broker_only_position(self, fresh_db):
        d = await rec.collect_and_report(_RecBridge(positions=[_pos(ticket=111)]))

        assert d is not None
        assert "broker_only_manual" in _kinds(d)

    async def test_a_FAILED_BROKER_READ_reports_NOTHING(self, fresh_db):
        """Half a picture is worse than none. If the broker read fails and we
        diffed anyway, every open trade would look like it had vanished and
        the report would be a page of false alarms."""
        d = await rec.collect_and_report(
            _RecBridge(error=RuntimeError("bridge down")))

        assert d is None

    async def test_a_None_POSITION_LIST_reports_nothing_either(self, fresh_db):
        """None is "could not read", not "no positions" -- the same
        distinction stage3/010 turns on."""
        d = await rec.collect_and_report(_RecBridge(positions=None))

        assert d is None

    async def test_no_bridge_is_a_no_op(self, fresh_db):
        assert await rec.collect_and_report(None) is None

    async def test_an_unconfigured_bridge_is_a_no_op(self, fresh_db):
        assert await rec.collect_and_report(
            _RecBridge(positions=[], configured=False)) is None

    async def test_it_never_raises_into_the_monitor_loop(self, fresh_db):
        """It runs inside the monitor cycle. An exception escaping here would
        take down position management, which is far worse than a missed
        report."""
        class _Nasty(_RecBridge):
            async def get_deal_history(self, days=7):
                raise RuntimeError("history exploded")

        assert await rec.collect_and_report(_Nasty(positions=[])) is None

    async def test_it_picks_up_signals_parked_as_unknown(self, fresh_db):
        """The link to stage3/020: a parked signal is only resolvable from
        broker truth, and this is the pass that looks."""
        from backend.src.db.database import db
        import time as _t
        with db() as conn:
            conn.execute(
                "INSERT INTO vantage_signals (signal_id,source_name,direction,"
                "entry_low,entry_high,stop_loss,lot_size,status,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                ("sig-parked", "Test", "BUY", 4000.0, 4002.0, 3990.0, 0.1,
                 "unknown", _t.time()))

        d = await rec.collect_and_report(_RecBridge(positions=[]))

        assert [e.signal_id for e in d.entries if e.signal_id] == ["sig-parked"]


# ── whose position is it? ────────────────────────────────────────────────────
#
# Simon's answer to 001-trading-defaults #6 (25 Aug, AFTER this spec was
# written) overrides the spec's "adopt it as recovered":
#
#   "A -- Watch it only: show it, track its profit, never touch it. Manual MT5
#    trades stay Simon's; the app still counts them toward exposure and the
#    risk limits, but never moves a stop or closes one."
#
# But "broker position with no DB row" is two different situations wearing the
# same shape:
#
#   * a trade SIMON placed by hand in MT5 -- his, watch only;
#   * a trade THE APP placed and then crashed before recording -- the app's
#     own orphan, and the crash-recovery case this whole task exists for.
#
# stage3/010 made them distinguishable: every order the app sends now carries
# "ea:" or "py:" plus the trade id. A position with neither is not ours.
# Collapsing the two would either abandon the app's own orphans or take over
# Simon's manual trades, and he has said in writing which of those he wants.

class TestWhoseOrphanIsIt:
    def test_a_position_with_OUR_comment_is_ours(self):
        from backend.src.services.broker.dedup import comment_for_bridge_order

        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=111,
                                   comment=comment_for_bridge_order(TRADE_ID))],
            broker_deals=[], db_open_trades=[])

        assert _kinds(d) == ["broker_only_ours"]

    def test_an_EA_leg_with_no_db_row_is_ours_too(self):
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=111, comment=f"ea:{TRADE_ID[:10]}a1")],
            broker_deals=[], db_open_trades=[])

        assert _kinds(d) == ["broker_only_ours"]

    def test_a_position_with_NO_comment_is_MANUAL(self):
        """Simon's own trade. Watch only."""
        d = rec.diff_snapshots(broker_positions=[_pos(ticket=111, comment="")],
                               broker_deals=[], db_open_trades=[])

        assert _kinds(d) == ["broker_only_manual"]

    @pytest.mark.parametrize("comment", [
        "", "[sl 4046.50]", "batchClose", "my manual trade", None,
    ])
    def test_anything_not_ours_is_manual(self, comment):
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=111, comment=comment)],
            broker_deals=[], db_open_trades=[])

        assert _kinds(d) == ["broker_only_manual"]

    def test_a_manual_position_is_flagged_NEVER_TOUCH(self):
        """The detail text is what a human reads before acting on the report.
        It has to say which of the two this is."""
        d = rec.diff_snapshots(broker_positions=[_pos(ticket=111, comment="")],
                               broker_deals=[], db_open_trades=[])

        assert "never" in d.entries[0].detail.lower()

    def test_BOTH_still_need_attention(self):
        """Watch-only does not mean ignore. A manual position still counts
        toward exposure and the risk limits, which is exactly why the report
        must show it."""
        ours = rec.diff_snapshots([_pos(ticket=1, comment=f"ea:{TRADE_ID[:10]}a1")],
                                  [], [])
        manual = rec.diff_snapshots([_pos(ticket=2, comment="")], [], [])

        assert ours.needs_attention is True
        assert manual.needs_attention is True

    def test_the_report_distinguishes_them(self):
        d = rec.diff_snapshots(
            broker_positions=[_pos(ticket=1, comment=f"py:{TRADE_ID[:10]}"),
                              _pos(ticket=2, comment="")],
            broker_deals=[], db_open_trades=[])

        text = rec.report(d)

        assert "broker_only_ours" in text
        assert "broker_only_manual" in text
