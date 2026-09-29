"""An adopted placeholder is handed to the EA at once (bug 070, 2026-09-29).

When the EA's `trade.Buy` times out at the broker it reports the open as
failed and forgets the trade. If the order fills afterwards, the placeholder
repair adopts the position by its order comment and the row becomes an
EA-managed trade with a ticket -- but the EA only learned of such rows on its
next `hello` (`_restore_open_trades`). Until then Python skipped the row
because the EA was healthy, and the EA did not know it: nothing managed it.

Adoption now sends the row to the EA with `restore_trade`, the same message
every `hello` sends. The EA ignores a ticket it already manages
(`FindManagedByTicket` in HandleRestoreTrade) and reports one that has closed,
so a second send is harmless.

Nothing here reaches a broker or an EA: the EA is a recorder and the bridge
the shared fake that refuses any close.
"""
import asyncio

import pytest

from backend.src.services.broker import ea_bridge
from backend.src.services.positions import core_template_placeholder_repair as repair
from tests.core.test_single_template_placeholder_expiry import (  # noqa: F401
    SINGLE, TRADE_ID, _insert, templates,
)
from tests.core.test_template_placeholder_repair import _FakeBridge

TICKET = 2107562994


class _RecordingEA:
    def __init__(self, healthy=True, fail=False):
        self._healthy = healthy
        self._fail = fail
        self.restored = []

    def is_ea_healthy(self):
        return self._healthy

    async def restore_trade(self, row):
        if self._fail:
            raise ConnectionError("socket closed")
        self.restored.append(row)


@pytest.fixture
def ea():
    before = ea_bridge.get_instance()
    yield
    ea_bridge.set_instance(before)


def _live_bridge():
    return _FakeBridge(positions=[{"ticket": TICKET, "open_price": 4147.00,
                                   "volume": 0.02, "comment": "ea:" + TRADE_ID[:12]}])


def _run():
    return asyncio.run(repair.repair_template_placeholders(_live_bridge()))


def test_the_adopted_position_is_sent_to_a_healthy_ea(templates, ea):
    rec = _RecordingEA()
    ea_bridge.set_instance(rec)
    _insert(SINGLE, age_s=60)

    assert _run() == 1

    assert len(rec.restored) == 1
    row = rec.restored[0]
    assert row["trade_id"] == TRADE_ID
    assert int(row["mt5_ticket"]) == TICKET
    assert float(row["entry_price"]) == 4147.00


def test_an_unhealthy_ea_is_not_sent_it(templates, ea):
    # Python reclaims a trade from an unhealthy EA (reclaim_ea_managed_trade),
    # and the EA's next hello restores it anyway.
    rec = _RecordingEA(healthy=False)
    ea_bridge.set_instance(rec)
    _insert(SINGLE, age_s=60)

    assert _run() == 1
    assert rec.restored == []


def test_no_ea_at_all_still_adopts(templates, ea):
    ea_bridge.set_instance(None)
    _insert(SINGLE, age_s=60)
    assert _run() == 1


def test_a_failed_send_does_not_undo_the_adoption(templates, ea):
    ea_bridge.set_instance(_RecordingEA(fail=True))
    _insert(SINGLE, age_s=60)

    assert _run() == 1

    from backend.src.db import database as db
    with db.db() as conn:
        ticket = conn.execute("SELECT mt5_ticket FROM vantage_simulated_trades "
                              "WHERE trade_id=?", (TRADE_ID,)).fetchone()[0]
    assert int(ticket) == TICKET


def test_the_owner_write_off_path_hands_it_over_too(templates, ea):
    rec = _RecordingEA()
    ea_bridge.set_instance(rec)
    _insert(SINGLE, age_s=11 * 3600)

    asyncio.run(repair.write_off_unconfirmed(_live_bridge()))

    assert [int(r["mt5_ticket"]) for r in rec.restored] == [TICKET]
