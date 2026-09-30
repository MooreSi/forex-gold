"""The Trades page's R column is realised R on every row, whichever node
opened the trade.

Owner report, 2026-09-30: "the R:R look all over the place and not
accurate". Two different quantities shared the column:

  * a trade THIS node opened: realised R, net P&L over the dollars the
    initial stop put at risk (`max_tp_repo.get_rr_map_by_ticket`);
  * a trade the OTHER node opened: the ledger's `rr`, which the frozen
    `close_trade` computes as |tp1 - entry| / |entry - stop_loss| from the
    stop AT CLOSE. Never negative, so a loss read as a good ratio, and after a
    breakeven or trail the stop sits next to entry and the ratio explodes: the
    demo ledger holds values up to 57.18 for trades that never made 3R.

`close_trade` may not be reshaped, so its `rr` is left as written and no
longer shown. The ledger gains `r_realised`, filled by the node that owns the
trade, and the Mac's periodic ledger pull carries it across.
"""
from __future__ import annotations

import pytest

from backend.src.db import database as db
from backend.src.services.analytics import ticket_maps
from backend.src.services.cluster import sync_repo
from backend.src.services.positions import max_tp as max_tp_svc

pytestmark = pytest.mark.usefixtures("fresh_db")


def _ledger_row(trade_id="T1", ticket="111", node=None, **over):
    t = {"trade_id": trade_id, "engine": "main", "direction": "BUY",
         "strategy": "template:x", "open_time": 1000.0, "close_time": 2000.0,
         "pnl_dollars": -50.0, "outcome": "loss", "tg_source": "Reversal Engine",
         "mt5_ticket": ticket, "max_tp_hit": None, "rr": None}
    t.update(over)
    sync_repo.record_consolidated_trade(node or sync_repo.get_or_create_node_id(), t)


def _local_trade(trade_id="T1", ticket=111, net=-50.0, initial_risk=25.0):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, direction, entry_low, entry_high, "
            "stop_loss, status, created_at) VALUES (?,?,?,?,?,?,?)",
            (f"sig-{trade_id}", "BUY", 4000.0, 4000.0, 3995.0, "active", 0.0))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id, signal_id, direction, "
            "entry_low, entry_high, entry_price, lot_size, remaining_lots, stop_loss, "
            "status, open_time, net_pnl, strategy, mt5_ticket, initial_risk) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, f"sig-{trade_id}", "BUY", 4000.0, 4000.0, 4000.0, 0.05, 0.0, 4000.0,
             "closed", 0.0, net, "template:x", ticket, initial_risk))


def _row(trade_id, node=None):
    node = node or sync_repo.get_or_create_node_id()
    return next(r for r in sync_repo.get_consolidated_trades()
                if r["trade_id"] == trade_id and r["node_id"] == node)


class TestTheLedgerCarriesRealisedR:
    """What the page reads (`ticket_maps._rr_map`) for a ticket this node
    has no local row for."""

    def test_realised_r_replaces_the_planned_ratio(self):
        """The close-time ratio of a trade that LOST: 57.18 is the largest
        value in the demo ledger on 2026-09-30. Once the owning node has
        filled in the real figure, that is what shows."""
        _ledger_row(rr=57.18, r_realised=-2.0, pnl_dollars=-50.0, node="vps-node")

        assert ticket_maps._rr_map()["111"] == pytest.approx(-2.0)

    def test_realised_r_is_shown(self):
        _ledger_row(r_realised=-2.0, node="vps-node")

        assert ticket_maps._rr_map()["111"] == pytest.approx(-2.0)

    def test_a_later_push_without_it_keeps_it(self):
        """The Max TP follow-up push and the reconnect replay both arrive
        without it; neither may blank it."""
        _ledger_row(r_realised=-2.0, node="vps-node")
        _ledger_row(max_tp_hit="TP1", node="vps-node")

        assert _row("T1", "vps-node")["r_realised"] == pytest.approx(-2.0)


class TestTheOwningNodeFillsItIn:
    def test_its_own_closed_trade_gets_net_over_initial_risk(self):
        _local_trade(net=-50.0, initial_risk=25.0)
        _ledger_row(rr=57.18)

        max_tp_svc.fill_realised_r()

        assert _row("T1")["r_realised"] == pytest.approx(-2.0)

    def test_the_other_nodes_rows_are_not_touched(self):
        """This node holds no risk figure for them; the owner fills its own."""
        _local_trade(trade_id="T2", ticket=222)
        _ledger_row(trade_id="T9", ticket="222", node="vps-node")

        max_tp_svc.fill_realised_r()

        assert _row("T9", "vps-node")["r_realised"] is None

    def test_a_trade_with_no_risk_figure_is_left_blank(self):
        """A blank says "not known". A guessed number is the bug being fixed."""
        _ledger_row(trade_id="T3", ticket="333")

        max_tp_svc.fill_realised_r()

        assert _row("T3")["r_realised"] is None

    def test_the_rest_of_the_row_survives(self):
        _local_trade(net=-50.0, initial_risk=25.0)
        _ledger_row(pnl_dollars=-50.0, max_tp_hit="n/a", tg_source="Reversal Engine")

        max_tp_svc.fill_realised_r()

        row = _row("T1")
        assert (row["pnl_dollars"], row["max_tp_hit"], row["tg_source"]) == \
            (-50.0, "n/a", "Reversal Engine")

    def test_a_filled_row_is_not_pushed_again(self, monkeypatch):
        """It runs every five minutes. Re-pushing the whole history each pass
        would flood the sync link."""
        _local_trade(net=-50.0, initial_risk=25.0)
        _ledger_row()
        pushed = []
        from backend.src.services.cluster.sync import ledger
        real = ledger.push_trade_closed
        monkeypatch.setattr(ledger, "push_trade_closed",
                            lambda t: (pushed.append(t["trade_id"]), real(t)))

        max_tp_svc.fill_realised_r()
        max_tp_svc.fill_realised_r()

        assert pushed == ["T1"]
