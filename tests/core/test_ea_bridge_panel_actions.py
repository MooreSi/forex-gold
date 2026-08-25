"""panel_action routing -- what a click on the on-chart panel is allowed to
do to the app.

The panel is a remote control, not a second trader: every control round-trips
through here, and the split below is the whole safety story.

  * a template FIELD name is written to the saved template and pushed back;
  * an ORDER action goes to the app's normal order paths and must never be
    mistaken for a field (a fall-through would write e.g. "close_all" into a
    template, which is silent and permanent);
  * select_channel changes only which template is being edited.
"""
import asyncio
from types import SimpleNamespace

import pytest

from backend.src.services.broker import ea_bridge


def _bridge(engine=None):
    b = ea_bridge.EABridge(engine=engine)
    b.sent = []

    async def _send(msg):
        b.sent.append(msg)
        return True

    b._send = _send
    return b


def _run(coro):
    return asyncio.run(coro)


def test_order_actions_never_reach_the_template_writer(monkeypatch):
    """The generic path writes any key in DEFAULTS. An order action reaching
    it would either be rejected as an unknown field (best case) or, if a
    field were ever named the same, silently corrupt a saved template."""
    b = _bridge()
    routed = []

    async def _fake_order(action, msg, template):
        routed.append(action)

    monkeypatch.setattr(b, "_on_panel_order", _fake_order)

    def _boom(*a, **k):
        raise AssertionError("template save must not be reached for an order")

    monkeypatch.setattr(ea_bridge.EABridge, "push_template", _boom, raising=True)

    for action in sorted(ea_bridge._ORDER_ACTIONS):
        _run(b._on_panel_action({"action": action, "template": "T"}))
    assert routed == sorted(ea_bridge._ORDER_ACTIONS)


def test_order_action_list_covers_every_entry_management_button():
    """Kept in step with OnChartEvent's Entry Management block in
    mql5/ForexTraderBridge.mq5 -- an action the EA sends but this set omits
    falls through to the template path instead of placing an order."""
    assert ea_bridge._ORDER_ACTIONS == {
        "market_buy", "market_sell", "limit_buy", "limit_sell",
        "close_all", "cancel_limits",
    }


def test_select_channel_moves_the_slot_and_re_pushes(monkeypatch):
    b = _bridge()
    pushed = []

    async def _tpl_for_slot():
        return {"name": f"tpl-{b._panel_slot}"}

    async def _push_template(name, template):
        pushed.append(name)
        return True

    async def _push_ctx():
        pushed.append("ctx")
        return True

    monkeypatch.setattr(b, "_template_for_selected_slot", _tpl_for_slot)
    monkeypatch.setattr(b, "push_template", _push_template)
    monkeypatch.setattr(b, "push_panel_context", _push_ctx)

    _run(b._on_panel_action({"action": "select_channel", "value": "2"}))
    assert b._panel_slot == 2
    assert pushed == ["tpl-2", "ctx"]


def test_select_channel_ignores_a_junk_slot():
    """The value crosses the wire as a string built by the EA; a malformed one
    must leave the selection alone rather than reset it to channel 1."""
    b = _bridge()
    b._panel_slot = 1
    _run(b._on_panel_action({"action": "select_channel", "value": "banana"}))
    assert b._panel_slot == 1


def test_negative_slot_is_clamped_not_wrapped():
    """A negative index would silently select the LAST channel via Python's
    negative indexing further down, which is a different channel's template
    than the one the user clicked."""
    b = _bridge()
    _run(b._on_panel_action({"action": "select_channel", "value": "-3"}))
    assert b._panel_slot == 0


def test_order_without_an_mt5_bridge_is_refused_and_reported():
    """The terminal has no other way to learn its click did nothing."""
    b = _bridge(engine=SimpleNamespace(_bridge=None, _cfg={}))
    b._last_seen = float("inf")   # pretend healthy so push_panel_log sends
    b._writer = object()
    _run(b._on_panel_order("market_buy", {}, "T"))
    assert b.sent and b.sent[-1]["type"] == "panel_log"
    assert "no MT5 bridge" in b.sent[-1]["text"]


def test_limit_order_without_a_price_is_refused(monkeypatch):
    """The EA computes the entry price from its own pip convention. If that
    field is missing, deriving one here would mean a second, divergent copy
    of the same arithmetic -- so it refuses instead."""
    b = _bridge(engine=SimpleNamespace(
        _bridge=SimpleNamespace(), _cfg={"starting_balance": 1000.0}))
    b._last_seen = float("inf")
    b._writer = object()
    _run(b._on_panel_order("limit_buy", {"sl": 3990.0}, "T"))
    assert "LIMIT REFUSED" in b.sent[-1]["text"]


def test_limit_order_without_any_tp_is_refused():
    b = _bridge(engine=SimpleNamespace(
        _bridge=SimpleNamespace(), _cfg={"starting_balance": 1000.0}))
    b._last_seen = float("inf")
    b._writer = object()
    _run(b._on_panel_order("limit_buy", {"price": 4000.0, "sl": 3990.0}, "T"))
    assert "no TP1" in b.sent[-1]["text"]


def test_panel_pushes_are_silent_when_no_ea_is_connected():
    """Nothing on the panel path is on a trading path, so an absent EA is a
    no-op, never an exception into the reader loop."""
    b = ea_bridge.EABridge(engine=None)
    assert _run(b.push_panel_context()) is False
    assert _run(b.push_panel_signal()) is False
    assert _run(b.push_panel_log("hi")) is False


def test_panel_signal_payload_is_flat_and_json_safe(monkeypatch):
    """The EA's JSON reader is a flat key scanner by design (JsonGetString in
    ForexTraderBridge.mq5). A nested value would parse as garbage rather than
    fail loudly, so levels are flattened and booleans are sent as 1/0."""
    from backend.src.services.positions import core_panel_signal as ps

    b = _bridge(engine=SimpleNamespace(_bridge=SimpleNamespace()))
    b._last_seen = float("inf")
    b._writer = object()

    async def _payload(_bridge):
        p = ps.empty_payload()
        p["fvg"] = True
        p["levels"] = [{"price": 4001.5, "kind": "swing", "dir": "BUY"}]
        return p

    monkeypatch.setattr(ps, "build_payload", _payload)
    _run(b.push_panel_signal())

    msg = b.sent[-1]
    assert msg["type"] == "panel_signal"
    assert msg["fvg"] == 1 and msg["sweep"] == 0
    assert msg["level_count"] == 1
    assert msg["lvl1_price"] == 4001.5 and msg["lvl1_dir"] == "BUY"
    assert "levels" not in msg
    assert all(not isinstance(v, (dict, list)) for v in msg.values())
