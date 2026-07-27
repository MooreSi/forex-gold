"""EABridge.open_trade()'s tpl_* wire fields (EA Templates) -- confirmed
live 2026-07-23 that tpl_cancel_pending/tpl_harvest_enabled sent as a
native Python bool serialize to JSON true/false, which the EA's minimal
JSON parser (numbers/strings only, see ForexTraderBridge.mq5's
JsonGetLong) silently reads back as 0 via StringToInteger("true") == 0 --
harvest and grid cancel-pending never fired regardless of the template's
actual setting. Must always be sent as a plain 1/0 int, matching every
other flag field in this protocol (close_full_on_last)."""
import asyncio
import json
import time

import pytest

from forex_trader.core import ea_bridge


class _FakeWriter:
    def __init__(self):
        self.written: list[bytes] = []

    def write(self, data: bytes) -> None:
        self.written.append(data)

    async def drain(self) -> None:
        return None


def _healthy_bridge():
    bridge = ea_bridge.EABridge(engine=None)
    bridge._writer = _FakeWriter()
    bridge._last_seen = time.time()
    return bridge


def _sent_message(bridge) -> dict:
    raw = bridge._writer.written[-1].decode().strip()
    return json.loads(raw)


_TEMPLATE = {
    "mode": "grid", "grid_step_pts": 5.0, "grid_legs": 2,
    "tpsl_mode": "on", "anchor": "unified", "trail_mode": "off",
    "be_mode": "entry", "be_buffer_pts": 1.0, "be_trigger": 1,
    "cancel_pending": True, "group_tp_action": True,
    "harvest_enabled": True, "harvest_threshold": 25.0,
}


def test_template_bool_flags_sent_as_int_not_native_bool():
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.open_trade(
            "trade-1", "BUY", 0.10, 2390.0, {}, "template:T1",
            template=_TEMPLATE, timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert msg["tpl_cancel_pending"] == 1
    assert type(msg["tpl_cancel_pending"]) is int
    assert msg["tpl_group_tp_action"] == 1
    assert type(msg["tpl_group_tp_action"]) is int
    assert msg["tpl_harvest_enabled"] == 1
    assert type(msg["tpl_harvest_enabled"]) is int


def test_template_bool_flags_false_sent_as_zero():
    bridge = _healthy_bridge()
    template = dict(_TEMPLATE, cancel_pending=False, group_tp_action=False, harvest_enabled=False)
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.open_trade(
            "trade-2", "BUY", 0.10, 2390.0, {}, "template:T1",
            template=template, timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert msg["tpl_cancel_pending"] == 0
    assert type(msg["tpl_cancel_pending"]) is int
    assert msg["tpl_group_tp_action"] == 0
    assert type(msg["tpl_group_tp_action"]) is int
    assert msg["tpl_harvest_enabled"] == 0
    assert type(msg["tpl_harvest_enabled"]) is int


# ── Grid zone-spanned staging (2026-07-28) ───────────────────────────────
# The signal's own entry zone is sent so HandleOpenTemplateGrid can stage
# legs ACROSS it instead of stepping grid_step_pts away from current price
# -- fixed stepping walks the legs through the signal's own SL and the
# broker rejects every one as invalid stops.

def test_zone_sent_when_signal_states_one():
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.open_trade(
            "trade-z1", "BUY", 0.10, 4062.0, {}, "template:T1",
            template=_TEMPLATE, zone_low=4063.0, zone_high=4068.0, timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert msg["zone_low"] == 4063.0
    assert msg["zone_high"] == 4068.0


def test_zone_omitted_when_absent_so_ea_falls_back_to_step_staging():
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.open_trade(
            "trade-z2", "BUY", 0.10, 4062.0, {}, "template:T1",
            template=_TEMPLATE, timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert "zone_low" not in msg
    assert "zone_high" not in msg


def test_degenerate_zone_is_not_sent():
    # entry_low == entry_high (a point, not a zone) must not be treated as
    # spannable -- every leg would land on the same price.
    bridge = _healthy_bridge()
    with pytest.raises(asyncio.TimeoutError):
        asyncio.run(bridge.open_trade(
            "trade-z3", "BUY", 0.10, 4062.0, {}, "template:T1",
            template=_TEMPLATE, zone_low=4065.0, zone_high=4065.0, timeout=0.05,
        ))
    msg = _sent_message(bridge)
    assert "zone_low" not in msg
