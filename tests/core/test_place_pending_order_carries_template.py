"""A resting order carries its template, on placement and after a restart.

docs/todo/limit-orders/030. Written RED, before the Python restore half.

`PendingOrder` in ForexTraderBridge.mq5 has carried every `tpl_*` field since
the grid path was written, and `CheckPendingOrders()` already copies all of
them into the `ManagedTrade` it promotes on a fill. The only thing standing
between a resting template order and being managed as one was
`HandlePlacePendingOrder`'s hardcoded `p.isTemplate = false` — and, on the
Python side, a `place_pending_order` that had no `template` parameter to send.

**`restore_pending_order` is the half that is easy to forget.** `g_pending[]`
is pure in-memory state on the EA; any restart — a recompile, a terminal
restart, a dropped socket — forgets every resting order, and Python pushes each
still-working row back on the next "hello". If that push omits the template,
an EA restart mid-rest silently demotes a template order to an untemplated one,
and the loss shows up only at the fill, possibly hours later. A recompile is
exactly what deploying this change requires, so the window is not hypothetical.

**The template is re-fetched by name, not stored on the row.**
`vantage_pending_orders.strategy` already holds `template:<name>`. Copying the
template's values into that table as well would be a second copy that goes
stale the moment the user edits the template — and the order would then be
restored under values that no longer exist anywhere in the UI.

There is no MQL5 test runner in this repo, so the EA half of 030 is verified by
reading and by the demo session. Said plainly rather than implied: nothing
below executes a line of MQL5.
"""
from __future__ import annotations

import asyncio
import json
import time

import pytest

from backend.src.services.broker import ea_bridge as ea_bridge_mod
from backend.src.services.broker import ea_templates
from backend.src.services.broker.ea_bridge import EABridge

TEMPLATE_NAME = "GD Instituational - single"
STRATEGY = f"template:{TEMPLATE_NAME}"
RESTING_PRICE = 4415.00
STOP_LOSS = 4409.00
TPS = {1: 4419.0, 2: 4424.0, 3: 4430.0}

TEMPLATE = {
    "mode": "single", "sl_pips": 60.0,
    "tp1_pips": 40.0, "tp2_pips": 90.0, "tp3_pips": 150.0,
    "trail_mode": "staged", "be_mode": "entry_buffer",
    "cancel_pending": True, "harvest_enabled": True,
}


class _Wire(EABridge):
    """An EABridge that records the message instead of sending it.

    Subclassed rather than mocked so the real `place_pending_order` /
    `restore_pending_order` bodies run — the whole question is what they put on
    the wire, and a mock would answer it with whatever this file imagined.
    """

    def __init__(self):
        self.sent: list[dict] = []

    def is_ea_healthy(self) -> bool:
        return True

    async def _send(self, msg: dict) -> bool:
        self.sent.append(dict(msg))
        return True


def _row(status="working"):
    return {
        "trade_id": "trade-aaaa", "signal_id": "sig-aaaa", "tg_message_id": "tg1",
        "channel_name": "GOLD DIGGERS INSTITUTIONAL", "direction": "BUY",
        "price": RESTING_PRICE, "stop_loss": STOP_LOSS,
        "tps_json": json.dumps({str(k): v for k, v in TPS.items()}),
        "pcts_json": json.dumps([0.25, 0.25, 0.25]), "be_at_pos": 0, "tp_open": 1,
        "lot_size": 0.10, "ea_ticket": 5551, "status": status,
        "created_at": time.time() - 300.0, "resolved_at": None,
        "strategy": STRATEGY,
    }


def _tpl_keys(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if k.startswith("tpl_")}


class TestPlacement:
    def test_the_template_goes_out_as_flat_tpl_fields(self, fresh_db):
        wire = _Wire()

        asyncio.run(_place(wire, template=dict(TEMPLATE)))

        tpl = _tpl_keys(wire.sent[0])
        assert tpl.get("tpl_sl_pips") == pytest.approx(60.0)
        assert tpl.get("tpl_trail_mode") == "staged"
        assert tpl.get("tpl_be_mode") == "entry_buffer"

    def test_booleans_are_sent_as_ints(self, fresh_db):
        """The EA's minimal JSON parser understands numbers and strings, not
        native booleans. Sending a Python bool evaluated false on the EA side
        (StringToInteger("true") == 0), which is how harvest and grid
        cancel-pending silently never fired — live 2026-07-23."""
        wire = _Wire()

        asyncio.run(_place(wire, template=dict(TEMPLATE)))

        tpl = _tpl_keys(wire.sent[0])
        assert tpl["tpl_cancel_pending"] == 1
        assert tpl["tpl_harvest_enabled"] == 1
        assert not any(isinstance(v, bool) for v in tpl.values()), (
            f"a native bool reached the wire: {tpl}"
        )

    def test_bookkeeping_fields_are_not_sent(self, fresh_db):
        wire = _Wire()

        asyncio.run(_place(wire, template=dict(TEMPLATE, name=TEMPLATE_NAME,
                                               created_at=1.0, updated_at=2.0)))

        tpl = _tpl_keys(wire.sent[0])
        assert "tpl_name" not in tpl
        assert "tpl_created_at" not in tpl
        assert "tpl_updated_at" not in tpl

    def test_no_template_means_no_tpl_keys_at_all(self, fresh_db):
        """Limit Runner and ORB place resting orders too. Their payload must be
        exactly what it was before 030."""
        wire = _Wire()

        asyncio.run(_place(wire, template=None))

        assert _tpl_keys(wire.sent[0]) == {}


class TestRestoreAfterAnEaRestart:
    def test_the_restore_carries_the_template_too(self, fresh_db):
        """The half that is easy to forget — and a recompile is exactly what
        deploying 030 requires, so the restart window is not hypothetical."""
        ea_templates.save_ea_template(TEMPLATE_NAME, dict(TEMPLATE))
        wire = _Wire()

        asyncio.run(wire.restore_pending_order(_row()))

        tpl = _tpl_keys(wire.sent[0])
        assert tpl, "an EA restart would put this order back with no template"
        assert tpl.get("tpl_sl_pips") == pytest.approx(60.0)
        assert tpl.get("tpl_trail_mode") == "staged"

    def test_it_carries_the_resting_price_the_ea_needs_as_its_anchor(self, fresh_db):
        """`ApplyTemplateToPending` measures breakeven and the trail from the
        order's own resting price. The restore payload never sent one."""
        ea_templates.save_ea_template(TEMPLATE_NAME, dict(TEMPLATE))
        wire = _Wire()

        asyncio.run(wire.restore_pending_order(_row()))

        assert wire.sent[0].get("price") == pytest.approx(RESTING_PRICE)

    def test_the_template_is_read_back_by_name_not_from_the_row(self, fresh_db):
        """Edited templates must restore under their CURRENT values. A copy
        stored beside the order would be stale the moment the user changed
        anything, and the order would resume under numbers that exist nowhere
        in the UI."""
        ea_templates.save_ea_template(TEMPLATE_NAME, dict(TEMPLATE))
        ea_templates.save_ea_template(TEMPLATE_NAME, dict(TEMPLATE, sl_pips=25.0))
        wire = _Wire()

        asyncio.run(wire.restore_pending_order(_row()))

        assert _tpl_keys(wire.sent[0])["tpl_sl_pips"] == pytest.approx(25.0)

    def test_a_non_template_order_restores_exactly_as_before(self, fresh_db):
        wire = _Wire()

        asyncio.run(wire.restore_pending_order(dict(_row(), strategy="limit_runner")))

        assert _tpl_keys(wire.sent[0]) == {}

    def test_a_template_that_no_longer_exists_does_not_break_the_restore(self, fresh_db):
        """A deleted template must not cost the order its tracking — that is
        the very gap restore_pending_order was written to close."""
        wire = _Wire()

        asyncio.run(wire.restore_pending_order(_row()))

        assert wire.sent, "the restore was abandoned because the template was gone"
        assert _tpl_keys(wire.sent[0]) == {}


async def _place(wire, template):
    """Send, then let the ack wait time out.

    `place_pending_order` sends and then blocks for the EA's acceptance ack,
    which nothing here will ever deliver. The message is on the wire before
    that wait begins, so the timeout is swallowed: what is under test is the
    payload, not the handshake, and the handshake has its own coverage in
    tests/core/test_ea_bridge_pending_orders.py.
    """
    try:
        return await wire.place_pending_order(
            "trade-aaaa", "BUY", RESTING_PRICE, 0.10, STOP_LOSS, TPS,
            [0.25, 0.25, 0.25], 0, strategy=STRATEGY, template=template,
            timeout=0.01,
        )
    except (asyncio.TimeoutError, asyncio.CancelledError):
        return None
