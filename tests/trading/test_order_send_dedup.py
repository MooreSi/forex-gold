"""Order-send de-duplication (stage3/010).

The hole this closes, from the 2026-08-08 risk review (C1): when the EA is
merely SLOW, `open_trade` times out waiting for its ack, the outer handler logs
"handoff failed -- falling back to Python bridge", and the bridge places a
SECOND order for a trade the EA may already have on the book.

Two things made it undetectable. Nothing queried the broker before that
fallback send, and the two send paths stamped DIFFERENT identifiers -- the EA
writes "ea:<trade_id[:10]>" on every leg, the bridge wrote "sig:<signal_id[:8]>"
-- so even a check could not have correlated them.

`find_trade` answers one question against the broker's own records: does this
trade_id already exist there? It returns three states, not two, and the third
is the point:

  FOUND    the broker shows it. Do not send.
  ABSENT   the broker is reachable and does not show it. Safe to send.
  UNKNOWN  the broker could not be asked. NOT the same as absent, and reading
           it as absent is how a retry doubles an order.

Everything here runs against fakes. No real or demo order is placed.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import dedup




TRADE_ID = "5b88a61e-6g3f-42"


class _Bridge:
    """Records what it was asked, returns what it was told to."""

    def __init__(self, positions=None, deals=None,
                 positions_error=None, deals_error=None):
        self._positions = positions if positions is not None else []
        self._deals = deals if deals is not None else []
        self._positions_error = positions_error
        self._deals_error = deals_error
        self.deal_days: list = []

    async def get_positions(self):
        if self._positions_error:
            raise self._positions_error
        return self._positions

    async def get_deal_history(self, days=7):
        self.deal_days.append(days)
        if self._deals_error:
            raise self._deals_error
        return self._deals


def _ea_leg(trade_id=TRADE_ID, ticket=111, suffix="a1"):
    return {"ticket": ticket, "comment": f"ea:{trade_id[:10]}{suffix}",
            "volume": 0.05, "open_price": 4000.0}


def _bridge_order(trade_id=TRADE_ID, ticket=222):
    return {"ticket": ticket, "comment": dedup.comment_for_bridge_order(trade_id),
            "volume": 0.05, "open_price": 4000.0}


class TestTheCommentCarriesTheTradeId:
    def test_the_bridge_comment_embeds_the_trade_id(self):
        """Without this the two send paths cannot be correlated at all --
        the bridge used to stamp the SIGNAL id while the EA stamped the TRADE
        id, so no check could have matched them."""
        c = dedup.comment_for_bridge_order(TRADE_ID)
        assert TRADE_ID[:10] in c

    def test_it_is_NOT_confusable_with_an_EA_leg(self):
        """Several services parse "ea:" comments to map a broker position back
        onto a template row. A bridge order wearing that prefix would be read
        as a leg of a template trade it has nothing to do with."""
        from backend.src.services.broker.ea_bridge import trade_id_prefix_from_comment

        c = dedup.comment_for_bridge_order(TRADE_ID)

        assert not c.startswith("ea:")
        assert trade_id_prefix_from_comment(c) is None

    def test_it_fits_the_brokers_comment_limit(self):
        """MT5 truncates order comments at 31 characters. A comment that gets
        cut mid-id stops matching and silently disables the dedup."""
        assert len(dedup.comment_for_bridge_order(TRADE_ID)) <= 31


@pytest.mark.asyncio
class TestFound:
    async def test_an_open_EA_leg_is_found(self):
        res = await dedup.find_trade(_Bridge(positions=[_ea_leg()]), TRADE_ID)

        assert res.found is True
        assert res.unknown is False
        assert res.ticket == 111

    async def test_it_reports_what_the_caller_needs_to_ADOPT(self):
        """Refusing to send is only half the job. Without the ticket and fill
        price the caller writes a row with no ticket, which orphans a live
        position -- the same shape as bugs/016."""
        res = await dedup.find_trade(_Bridge(positions=[_ea_leg()]), TRADE_ID)

        assert res.ticket == 111
        assert res.entry_price == 4000.0

    async def test_it_reports_WHICH_path_placed_it(self):
        """An EA leg is EA-managed; a bridge order is not. Recording the wrong
        one points the management loop at the wrong owner."""
        ea = await dedup.find_trade(_Bridge(positions=[_ea_leg()]), TRADE_ID)
        py = await dedup.find_trade(_Bridge(positions=[_bridge_order()]), TRADE_ID)

        assert ea.by_ea is True
        assert py.by_ea is False

    async def test_an_open_BRIDGE_order_is_found(self):
        res = await dedup.find_trade(_Bridge(positions=[_bridge_order()]), TRADE_ID)

        assert res.found is True
        assert res.ticket == 222

    async def test_it_scans_recent_DEALS_not_just_open_positions(self):
        """The trade filled and closed again while the ack was outstanding.
        Checking only open positions would report absent and re-send."""
        deals = [{"position_id": 55, "order": 333, "entry": 0,
                  "comment": f"ea:{TRADE_ID[:10]}a1", "price": 4000.0,
                  "volume": 0.05, "time": 1000}]

        res = await dedup.find_trade(_Bridge(deals=deals), TRADE_ID)

        assert res.found is True
        assert res.source == "deal"

    async def test_a_position_is_preferred_over_a_deal(self):
        """If both exist, the live position is the useful answer -- it is what
        the caller adopts."""
        deals = [{"position_id": 55, "order": 333, "entry": 0,
                  "comment": f"ea:{TRADE_ID[:10]}a1", "time": 1000}]

        res = await dedup.find_trade(_Bridge(positions=[_ea_leg()], deals=deals),
                                     TRADE_ID)

        assert res.source == "position"
        assert res.ticket == 111

    async def test_any_leg_of_a_grid_counts_as_found(self):
        """A template stages several legs under one trade_id. Finding the
        third one is still proof the send happened."""
        res = await dedup.find_trade(
            _Bridge(positions=[_ea_leg(suffix="g3", ticket=999)]), TRADE_ID)

        assert res.found is True


@pytest.mark.asyncio
class TestAbsent:
    async def test_an_empty_broker_is_absent(self):
        res = await dedup.find_trade(_Bridge(), TRADE_ID)

        assert res.found is False
        assert res.unknown is False
        assert res.safe_to_send is True

    async def test_ANOTHER_TRADES_position_does_not_count(self):
        """The negative control the spec asks for. If this reported found,
        every assertion above would pass for the wrong reason and the gate
        would block every order forever."""
        other = _ea_leg(trade_id="ffffffff-ffff-ff", ticket=777)

        res = await dedup.find_trade(_Bridge(positions=[other]), TRADE_ID)

        assert res.found is False

    async def test_broker_written_comments_are_ignored(self):
        """MT5 writes its own comments on stop-outs ("[sl 4046.50]") and
        partial closes ("batchClose"). None of them is one of ours."""
        noise = [{"ticket": 1, "comment": "[sl 4046.50]"},
                 {"ticket": 2, "comment": "batchClose"},
                 {"ticket": 3, "comment": ""},
                 {"ticket": 4}]

        res = await dedup.find_trade(_Bridge(positions=noise), TRADE_ID)

        assert res.found is False

    async def test_a_CLOSING_deal_alone_is_not_an_open(self):
        """entry != 0 is an exit. Only an opening deal proves an order was
        placed under this id."""
        deals = [{"position_id": 55, "entry": 1,
                  "comment": f"ea:{TRADE_ID[:10]}a1", "time": 1000}]

        res = await dedup.find_trade(_Bridge(deals=deals), TRADE_ID)

        assert res.found is False


@pytest.mark.asyncio
class TestUnknownIsNotAbsent:
    """The state that matters most. A broker that cannot be asked has NOT
    said no."""

    async def test_a_failing_position_query_is_unknown(self):
        res = await dedup.find_trade(
            _Bridge(positions_error=RuntimeError("bridge down")), TRADE_ID)

        assert res.unknown is True
        assert res.found is False

    async def test_a_failing_deal_query_is_unknown_too(self):
        """Positions came back empty, but the deals half failed -- so "not in
        positions" is only half an answer."""
        res = await dedup.find_trade(
            _Bridge(deals_error=RuntimeError("history unavailable")), TRADE_ID)

        assert res.unknown is True

    async def test_positions_returning_None_is_unknown_not_empty(self):
        """The bridge returns None when it cannot read. An empty list means
        "nothing there"; None means "could not look", and treating them alike
        is exactly how a retry doubles an order."""
        res = await dedup.find_trade(_NoneBridge(), TRADE_ID)

        assert res.unknown is True

    async def test_a_found_position_beats_a_failing_deal_query(self):
        """Already proven found; the deals half no longer matters."""
        res = await dedup.find_trade(
            _Bridge(positions=[_ea_leg()],
                    deals_error=RuntimeError("history unavailable")), TRADE_ID)

        assert res.found is True
        assert res.unknown is False


class _NoneBridge:
    async def get_positions(self):
        return None

    async def get_deal_history(self, days=7):
        return []


@pytest.mark.asyncio
class TestTheDealWindow:
    async def test_it_asks_for_a_bounded_window(self):
        """A full history scan on every fallback send would be slow at exactly
        the moment the app is already struggling."""
        b = _Bridge()

        await dedup.find_trade(b, TRADE_ID)

        assert b.deal_days, "the deal history was never queried"
        assert 0 < b.deal_days[0] <= 7

    async def test_the_window_is_configurable(self):
        b = _Bridge()

        await dedup.find_trade(b, TRADE_ID, deal_days=3)

        assert b.deal_days == [3]


@pytest.mark.asyncio
class TestGuards:
    async def test_an_empty_trade_id_is_never_found(self):
        """A blank id would prefix-match every comment and block all trading."""
        res = await dedup.find_trade(_Bridge(positions=[_ea_leg()]), "")

        assert res.found is False

    async def test_an_empty_trade_id_does_not_even_ASK_the_broker(self):
        """The guard is meant to fail fast. Removing it still answers "not
        found" -- an empty prefix tuple matches nothing -- so only the absence
        of the round trip distinguishes the two, and mutation proved the
        earlier assertion could not."""
        b = _Bridge(positions=[_ea_leg()])

        await dedup.find_trade(b, "")

        assert b.deal_days == [], "the broker was queried for a blank trade id"

    async def test_no_bridge_is_unknown_not_absent(self):
        res = await dedup.find_trade(None, TRADE_ID)

        assert res.unknown is True
        assert res.found is False


# ── The gate in open_trade itself ────────────────────────────────────────────
#
# The tests above prove find_trade answers correctly. These prove open_trade
# ACTS on the answer, which is the part that stops the second order.

class _Tick:
    bid = 3999.0
    ask = 4000.0


class _GateBridge:
    """A bridge that records every order it is asked to place."""

    def __init__(self, positions=None, deals=None):
        self.placed: list = []
        self._positions = positions or []
        self._deals = deals or []

    def is_configured(self):
        return True

    async def get_positions(self):
        return self._positions

    async def get_deal_history(self, days=7):
        return self._deals

    async def get_fresh_tick(self):
        return _Tick()

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.placed.append({"direction": direction, "lots": lots, "sl": sl,
                            "tp": tp, "comment": comment})
        return {"ticket": 424242, "fill_price": 4000.0}


@pytest.mark.asyncio
class TestTheFallbackGate:
    """`open_trade`'s EA handoff can time out while the EA is merely slow. The
    fallback below it must not fire blind."""

    async def test_the_bridge_send_carries_the_TRADE_id(self, monkeypatch):
        """It used to carry the SIGNAL id, which is a different value from the
        one the EA stamps -- so the two paths could never be correlated."""
        from backend.src.services.trading import open_trade as ot

        comment = ot._bridge_order_comment("5b88a61e-6g3f-42", "sig-99")

        assert "5b88a61e-6" in comment

    async def test_a_found_trade_is_ADOPTED_and_NOTHING_IS_SENT(self):
        """The whole point of stage3/010."""
        from backend.src.services.trading import open_trade as ot

        bridge = _GateBridge(positions=[
            {"ticket": 111, "comment": f"ea:{TRADE_ID[:10]}a1",
             "open_price": 4001.5, "volume": 0.05}])

        decision = await ot._resolve_fallback_send(bridge, TRADE_ID,
                                                   ea_attempted=True)

        assert decision.send is False
        assert decision.ticket == 111
        assert decision.entry_price == 4001.5
        assert bridge.placed == [], "a second order was placed"

    async def test_an_absent_trade_IS_sent(self):
        from backend.src.services.trading import open_trade as ot

        decision = await ot._resolve_fallback_send(_GateBridge(), TRADE_ID,
                                                   ea_attempted=True)

        assert decision.send is True
        # Not merely "send": a confirmed-absent broker must not be reported as
        # unknown. Both states send today, so without this the unknown branch
        # could swallow the absent case and nothing would notice -- mutation
        # showed exactly that.
        assert decision.unknown is False

    async def test_the_gate_is_SKIPPED_when_the_EA_was_never_asked(self):
        """No EA attempt means nothing could have been placed behind our back,
        so the ordinary path must not pay for a broker round trip."""
        from backend.src.services.trading import open_trade as ot

        bridge = _GateBridge(positions=[
            {"ticket": 111, "comment": f"ea:{TRADE_ID[:10]}a1"}])

        decision = await ot._resolve_fallback_send(bridge, TRADE_ID,
                                                   ea_attempted=False)

        assert decision.send is True, "the gate ran when no EA order was attempted"

    async def test_an_UNKNOWN_broker_PARKS_rather_than_sending(self):
        """This test used to assert the opposite, and said so: while 010 stood
        alone there was nowhere safe to put a signal we could not resolve, so
        it sent and logged loudly. Refusing would have left the signal
        'pending' and PendingWatcher would have re-activated it every 20s --
        the failure that turned 5 signals into ~133 opens on 2026-07-30.

        stage3/020 added the 'unknown' park, so the safe answer is now
        available: an unreachable broker has not said the trade is absent, and
        we stop instead of guessing."""
        from backend.src.services.trading import open_trade as ot
        from backend.src.services.trading.send_dedup import SendOutcomeUnknown

        class _Broken(_GateBridge):
            async def get_positions(self):
                raise RuntimeError("bridge down")

        broken = _Broken()

        with pytest.raises(SendOutcomeUnknown):
            await ot._resolve_fallback_send(broken, TRADE_ID, ea_attempted=True)

        assert broken.placed == [], "an order was sent while the broker was unreachable"
