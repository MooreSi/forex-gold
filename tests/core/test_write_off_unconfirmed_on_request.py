"""The owner can write off placeholders the broker has never heard of.

2026-09-28, owner: "need to remove the (+2 unconfirmed: in the VPS database,
no broker ticket) -- these are blocking 2 available slots". Two "30 TP1 SL50
and Trail" rows from 09:36 and 09:39, ticket 0 and entry 0, still open at
20:50. The VPS's reconciler says of both "open in the database, and the broker
has no record of it". They wait out the 24h expiry because this node cannot
tell the template is single mode (the Mac has no template of that name at
all), and 24h is what an unknown mode gets.

On request the write-off skips the MODE question and keeps the EVIDENCE
check: a live leg is still adopted, an opening deal is still left for the
repair pass, and anything younger than the single-mode expiry is still left
alone. Same close as the automatic write-off (record_close at 0.0,
"no_fill_expired"). The fake bridge refuses any close, so nothing here can
reach a broker.
"""
import asyncio

import pytest

from backend.src.services.positions import core_template_placeholder_repair as repair
from tests.core.test_single_template_placeholder_expiry import (  # noqa: F401
    TRADE_ID, _insert, _status, templates,
)
from tests.core.test_template_placeholder_repair import _FakeBridge

OLD = 11 * 3600       # the two VPS rows were ~11h old when this was asked for


def _write_off(bridge=None):
    return asyncio.run(repair.write_off_unconfirmed(bridge or _FakeBridge()))


def test_an_unknown_template_with_no_broker_record_is_written_off(templates):
    _insert("Deleted Template", age_s=OLD)

    result = _write_off()

    assert result["written_off"] == [TRADE_ID]
    assert tuple(_status()) == ("closed", "no_fill_expired")


def test_a_grid_one_is_written_off_too_when_asked(templates):
    """The owner's call, made looking at MT5. The 24h automatic expiry for a
    grid is unchanged (test_single_template_placeholder_expiry.py)."""
    from tests.core.test_single_template_placeholder_expiry import GRID
    _insert(GRID, age_s=OLD)

    assert _write_off()["written_off"] == [TRADE_ID]


def test_it_frees_the_slot(templates):
    from backend.src.services.trading import signal_state_repo
    _insert("Deleted Template", age_s=OLD)
    assert signal_state_repo.count_trade_slots_used() == 1

    _write_off()

    assert signal_state_repo.count_trade_slots_used() == 0


def test_a_live_leg_is_adopted_not_written_off(templates):
    comment = repair._comment_prefix(TRADE_ID) + "a1"
    bridge = _FakeBridge(positions=[{"ticket": 2103198838, "open_price": 4151.03,
                                     "volume": 0.03, "comment": comment}])
    _insert("Deleted Template", age_s=OLD)

    result = _write_off(bridge)

    assert result["written_off"] == []
    status, reason = _status()
    assert status == "open" and reason != "no_fill_expired"


def test_one_with_an_opening_deal_is_kept(templates):
    comment = repair._comment_prefix(TRADE_ID) + "a1"
    bridge = _FakeBridge(deals=[{"comment": comment, "entry": 0, "position_id": 9,
                                 "price": 4150.0, "volume": 0.03, "order": 9}])
    _insert("Deleted Template", age_s=OLD)

    result = _write_off(bridge)

    assert result["written_off"] == []
    assert _status()[0] == "open"
    assert result["kept"][0]["trade_id"] == TRADE_ID


def test_a_new_one_is_kept(templates):
    """The EA's ack can take a minute; under the single-mode expiry the order
    may still be on its way."""
    _insert("Deleted Template",
            age_s=repair.placeholder_single_no_fill_expiry_secs() - 60)

    result = _write_off()

    assert result["written_off"] == []
    assert _status()[0] == "open"


def test_an_unreadable_broker_writes_off_nothing(templates):
    class _Blind(_FakeBridge):
        async def get_positions(self):
            return None

    _insert("Deleted Template", age_s=OLD)

    result = _write_off(_Blind())

    assert result["written_off"] == []
    assert _status()[0] == "open"
    assert "broker" in result["error"].lower()
