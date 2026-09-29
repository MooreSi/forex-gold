"""A placeholder whose order MT5 still holds is not written off (bug 070).

2026-09-29, demo account 26004592: Vantage-Demo stopped answering trade
requests, and the terminal showed three market BUYs in state "started" --
sent, not yet accepted, filled or rejected -- for minutes. MT5 waits 3 minutes
on each request and queues the next behind it.

A single-mode template placeholder is written off after 300 s with no broker
position and no broker deal, and the owner's write-off button uses the same
300 s floor. An order in flight is neither a position nor a deal, so the row
went while the order could still fill. A fill after that has no row: nothing
manages it.

Now the order list is read too. A matching order keeps the row; an order list
that cannot be read keeps it as well, because a broker that could not be asked
has not said no. An order for some other trade changes nothing.

Nothing here reaches a broker -- the shared fake refuses any close and only
returns canned lists.
"""
import asyncio

from backend.src.services.positions import core_template_placeholder_repair as repair
from tests.core.test_single_template_placeholder_expiry import (  # noqa: F401
    SINGLE, TRADE_ID, _insert, _status, templates,
)
from tests.core.test_template_placeholder_repair import _FakeBridge

OLD = repair.placeholder_single_no_fill_expiry_secs() + 60


class _OrdersBridge(_FakeBridge):
    """The shared fake, plus the order list. `orders=None` is an unreadable one."""

    def __init__(self, orders=None, **kw):
        super().__init__(**kw)
        self._orders = orders

    async def get_orders(self):
        return self._orders


def _in_flight(trade_id=TRADE_ID):
    # The EA's single-open comment: "ea:" + the first 12 characters.
    return {"ticket": 2107562994, "type": 0, "state": 0,
            "comment": "ea:" + trade_id[:12], "volume": 0.02}


def _auto(bridge):
    return asyncio.run(repair.repair_template_placeholders(bridge))


def _write_off(bridge):
    return asyncio.run(repair.write_off_unconfirmed(bridge))


# ── The automatic expiry ─────────────────────────────────────────────────────

def test_an_order_still_in_flight_keeps_the_placeholder(templates):
    _insert(SINGLE, age_s=OLD)
    assert _auto(_OrdersBridge(orders=[_in_flight()])) == 0
    assert _status()[0] == "open"


def test_an_unreadable_order_list_keeps_the_placeholder(templates):
    _insert(SINGLE, age_s=OLD)
    assert _auto(_OrdersBridge(orders=None)) == 0
    assert _status()[0] == "open"


def test_an_order_for_another_trade_does_not_keep_it(templates):
    _insert(SINGLE, age_s=OLD)
    other = _in_flight("ffffffff-ffff-ff")
    assert _auto(_OrdersBridge(orders=[other])) == 1
    assert tuple(_status()) == ("closed", "no_fill_expired")


def test_no_orders_at_all_expires_it_as_before(templates):
    _insert(SINGLE, age_s=OLD)
    assert _auto(_OrdersBridge(orders=[])) == 1
    assert tuple(_status()) == ("closed", "no_fill_expired")


def test_a_grid_legs_resting_limit_order_also_counts(templates):
    # "ea:" + 10 characters + the leg: the prefix the repair already matches.
    _insert(SINGLE, age_s=OLD)
    leg = dict(_in_flight(), comment=repair._comment_prefix(TRADE_ID) + "g2", type=2, state=1)
    assert _auto(_OrdersBridge(orders=[leg])) == 0
    assert _status()[0] == "open"


# ── The owner's write-off ────────────────────────────────────────────────────

def test_the_write_off_keeps_one_whose_order_is_in_flight(templates):
    _insert(SINGLE, age_s=11 * 3600)
    result = _write_off(_OrdersBridge(orders=[_in_flight()]))
    assert result["written_off"] == []
    assert result["kept"][0]["trade_id"] == TRADE_ID
    assert "order" in result["kept"][0]["reason"]
    assert _status()[0] == "open"


def test_the_write_off_writes_nothing_off_when_orders_cannot_be_read(templates):
    _insert(SINGLE, age_s=11 * 3600)
    result = _write_off(_OrdersBridge(orders=None))
    assert result["written_off"] == []
    assert "Nothing was written off" in (result["error"] or "")
    assert _status()[0] == "open"


def test_the_write_off_still_writes_off_with_no_matching_order(templates):
    _insert(SINGLE, age_s=11 * 3600)
    result = _write_off(_OrdersBridge(orders=[_in_flight("ffffffff-ffff-ff")]))
    assert result["written_off"] == [TRADE_ID]
