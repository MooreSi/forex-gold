"""Handing a still-open position back to the EA after it reconnects.

The EA keeps its open positions in memory. Any restart — a recompile, a
terminal restart, a dropped socket re-triggering OnInit — silently forgot every
one of them: no partial closes, no breakeven, no trailing, and no close
notification either, since the app learns a trade closed from the EA's own
message. The row then stayed `open` in the database forever.

Confirmed live 2026-08-04, ticket 1704757612: a recompile at 15:30 orphaned it,
it closed at the broker at 16:13 for +$35, and the trades table still read
`status='open' remaining_lots=0.1 net_pnl=0` afterwards. That got worse once
`close_full_on_last=false` legitimately started leaving positions with no
broker-side TP — an orphan then has nothing at all to close it.

The property that costs money if it is wrong is **`remaining_lots`**. The EA
uses it to work out how much of the ladder already fired. Send the original
size instead and the restore re-runs a partial close that has already happened:
lots closed twice, on a position that no longer has them.

Nothing here reaches MetaTrader — `_send` records the payload.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker.ea_bridge import _restore

pytestmark = pytest.mark.asyncio


def _row(**over):
    row = {
        "trade_id": "abc123", "mt5_ticket": 1704757612, "direction": "buy",
        "entry_price": 2400.5, "lot_size": 0.10, "remaining_lots": 0.04,
        "stop_loss": 2392.0, "strategy": "scale_out",
        "tp1": 2410.0, "tp2": 2418.0, "tp3": None, "tp4": None,
        "tp5": None, "tp6": None, "tp7": None, "tp8": None,
    }
    row.update(over)
    return row


class _Node(_restore.RestoreMixin):
    def __init__(self):
        self.sent: list = []

    async def _send(self, msg):
        self.sent.append(msg)


@pytest.fixture
def node():
    return _Node()


@pytest.fixture
def no_templates(monkeypatch):
    """Default: not a template strategy."""
    from backend.src.services.broker import ea_templates
    monkeypatch.setattr(ea_templates, "is_template_override", lambda s: False)
    return ea_templates


class TestWhatTheEaIsToldAboutThePosition:

    async def test_it_is_a_restore_for_the_right_ticket(self, node, no_templates):
        await node.restore_trade(_row())

        msg = node.sent[0]
        assert msg["type"] == "restore_trade"
        assert msg["ticket"] == 1704757612
        assert msg["trade_id"] == "abc123"

    async def test_it_sends_WHAT_IS_LEFT_not_the_original_size(self, node,
                                                              no_templates):
        """The one that costs money. The EA derives how much of the ladder has
        already fired from this. Sending the original size re-runs a partial
        close that already happened."""
        await node.restore_trade(_row(lot_size=0.10, remaining_lots=0.04))

        msg = node.sent[0]
        assert msg["remaining_lots"] == pytest.approx(0.04)
        assert msg["orig_lots"] == pytest.approx(0.10)
        assert msg["remaining_lots"] != msg["orig_lots"], (
            "the restore would re-run partial closes that already fired"
        )

    async def test_the_direction_is_upper_cased_for_the_ea(self, node,
                                                          no_templates):
        await node.restore_trade(_row(direction="buy"))

        assert node.sent[0]["direction"] == "BUY"

    async def test_the_current_stop_goes_with_it(self, node, no_templates):
        """Not the original stop — a restore that resets a trailed or
        breakeven-moved stop back to its opening value widens the risk on a
        trade that had already de-risked."""
        await node.restore_trade(_row(stop_loss=2400.5))

        assert node.sent[0]["stop_loss"] == pytest.approx(2400.5)

    async def test_only_the_tps_that_exist_are_sent(self, node, no_templates):
        await node.restore_trade(_row())

        msg = node.sent[0]
        assert msg["tp1"] == 2410.0 and msg["tp2"] == 2418.0
        for n in range(3, 9):
            assert f"tp{n}" not in msg, (
                f"tp{n} is empty on the row; sending it as 0 would give the EA "
                f"a target at zero"
            )

    async def test_a_row_with_no_tps_at_all_still_restores(self, node,
                                                          no_templates):
        """`close_full_on_last=false` legitimately leaves positions with no
        broker-side TP. Those are the orphans with nothing to close them, so
        they are the ones that most need restoring."""
        await node.restore_trade(_row(**{f"tp{n}": None for n in range(1, 9)}))

        assert node.sent[0]["ticket"] == 1704757612


class TestTemplateSettingsComeFreshFromTheDatabase:
    """A restored trade is managed by the template's CURRENT settings, not
    whatever was cached when it opened."""

    @pytest.fixture
    def template(self, monkeypatch):
        from backend.src.services.broker import ea_templates
        tpl = {"name": "Asian - Grid", "created_at": 1, "updated_at": 2,
               "anchors": 1, "pendings": 3, "use_trailing": True,
               "close_full_on_last": False,
               "tp1_pct": 50.0, "tp2_pct": 30.0, "tp3_pct": 20.0}
        monkeypatch.setattr(ea_templates, "is_template_override", lambda s: True)
        monkeypatch.setattr(ea_templates, "template_name_from_override",
                            lambda s: "Asian - Grid")
        monkeypatch.setattr(ea_templates, "get_ea_template", lambda n: tpl)
        return tpl

    async def test_template_fields_are_prefixed_and_sent(self, node, template):
        await node.restore_trade(_row(strategy="template:Asian - Grid"))

        msg = node.sent[0]
        assert msg["tpl_anchors"] == 1
        assert msg["tpl_pendings"] == 3

    async def test_booleans_become_ONE_AND_ZERO(self, node, template):
        """MQL5 reads these as numbers. A JSON `true` arrives as something it
        does not understand, and the setting silently does not apply."""
        msg_before = len(node.sent)
        await node.restore_trade(_row(strategy="template:Asian - Grid"))

        msg = node.sent[msg_before]
        assert msg["tpl_use_trailing"] == 1
        assert msg["tpl_close_full_on_last"] == 0
        assert not isinstance(msg["tpl_use_trailing"], bool)

    async def test_bookkeeping_columns_are_not_sent(self, node, template):
        await node.restore_trade(_row(strategy="template:Asian - Grid"))

        msg = node.sent[0]
        for k in ("tpl_name", "tpl_created_at", "tpl_updated_at"):
            assert k not in msg

    async def test_the_partial_close_percentages_are_sent_as_fractions(
            self, node, template):
        """The template stores 50.0 meaning 50%. The EA wants 0.5. Sending 50
        would ask it to close fifty times the position."""
        await node.restore_trade(_row(strategy="template:Asian - Grid"))

        msg = node.sent[0]
        assert msg["pct1"] == pytest.approx(0.5)
        assert msg["pct2"] == pytest.approx(0.3)
        assert msg["pct3"] == pytest.approx(0.2)

    async def test_a_missing_template_still_restores_the_position(self, node,
                                                                  monkeypatch):
        """The template may have been deleted since the trade opened. The
        position is still live, so it must still be handed back rather than
        left orphaned."""
        from backend.src.services.broker import ea_templates
        monkeypatch.setattr(ea_templates, "is_template_override", lambda s: True)
        monkeypatch.setattr(ea_templates, "template_name_from_override",
                            lambda s: "Gone")
        monkeypatch.setattr(ea_templates, "get_ea_template", lambda n: None)

        await node.restore_trade(_row(strategy="template:Gone"))

        assert node.sent[0]["ticket"] == 1704757612


class TestLadderStrategiesGetTheirOwnShape:

    async def test_the_percentages_follow_how_many_TPs_the_row_HAS(
            self, node, no_templates):
        """A trade opened with two TPs must be restored on the two-TP ladder,
        not the table's largest — otherwise the EA closes different fractions
        than it did before the restart."""
        from backend.src.services.trading.open_trade import _EA_LADDER_PCTS

        strategy = next(iter(_EA_LADDER_PCTS))
        expected = _EA_LADDER_PCTS[strategy].get(
            2, _EA_LADDER_PCTS[strategy][max(_EA_LADDER_PCTS[strategy])])

        await node.restore_trade(_row(strategy=strategy))

        msg = node.sent[0]
        for i, p in enumerate(expected, start=1):
            assert msg[f"pct{i}"] == pytest.approx(p)
