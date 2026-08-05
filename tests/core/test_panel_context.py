"""The panel's channel roster (core_panel_context).

The CH tabs are a selector, not a config store. What matters here is that the
payload the terminal receives describes channels it can only *choose* between,
and that choosing one resolves to the right saved template -- picking the
wrong one would silently edit a different channel's live settings.
"""
from types import SimpleNamespace

from forex_trader.core import core_panel_context as pc


class _Reader:
    def __init__(self, slots, auth="connected"):
        self._slots = slots
        self._auth = auth

    def get_status(self):
        return {"auth_state": self._auth, "slots": self._slots}


def _slot(n, name, gid, listener=True, poller=False):
    return {"slot": n, "group_id": gid, "group_name": name,
            "listener_active": listener, "poller_active": poller}


def test_slots_map_to_ch_keys_in_reader_order(monkeypatch):
    """CH1 on the chart must be CH1 on the Telegram page. Any reordering here
    silently retargets every edit made from the panel."""
    monkeypatch.setattr(pc, "_template_for_channel", lambda n: f"T-{n}")
    ctx = pc.build_context(_Reader([_slot(1, "VIP", 111), _slot(2, "INST", 222)]))
    assert ctx["channel_count"] == 2
    assert ctx["ch1_name"] == "VIP" and ctx["ch1_id"] == "111"
    assert ctx["ch2_name"] == "INST" and ctx["ch2_id"] == "222"
    assert ctx["ch1_template"] == "T-VIP"


def test_a_slot_is_active_on_listener_or_poller(monkeypatch):
    """The push listener and the polling fallback are alternatives, not both
    required -- an AND here would show a working channel as idle."""
    monkeypatch.setattr(pc, "_template_for_channel", lambda n: "")
    ctx = pc.build_context(_Reader([
        _slot(1, "A", 1, listener=False, poller=True),
        _slot(2, "B", 2, listener=False, poller=False),
    ]))
    assert ctx["ch1_active"] == 1
    assert ctx["ch2_active"] == 0


def test_unnamed_slots_are_dropped(monkeypatch):
    """An unconfigured Telegram slot has no group name. Rendering it as an
    empty CH tab gives the user something to click that cannot resolve."""
    monkeypatch.setattr(pc, "_template_for_channel", lambda n: "")
    ctx = pc.build_context(_Reader([_slot(1, "A", 1), _slot(2, "", None)]))
    assert ctx["channel_count"] == 1


def test_no_reader_falls_back_to_template_assigned_channels(monkeypatch):
    """Telegram off is a normal configuration. The panel should still have a
    template to edit rather than going blank."""
    from forex_trader.core import database as db_module
    monkeypatch.setattr(db_module, "get_all_channel_strategy_overrides",
                        lambda: {"GD VIP": {"strategy": "template:Grid A", "auto": False},
                                 "Other":  {"strategy": None, "auto": False}})
    ctx = pc.build_context(None)
    assert ctx["channel_count"] == 1
    assert ctx["ch1_name"] == "GD VIP"
    assert ctx["ch1_template"] == "Grid A"
    assert ctx["tg_active"] == 0


def test_context_carries_no_writable_channel_fields(monkeypatch):
    """Every key is a mirror. If a future edit adds something the terminal
    could write back, this is the test that should have to change first."""
    monkeypatch.setattr(pc, "_template_for_channel", lambda n: "T")
    ctx = pc.build_context(_Reader([_slot(1, "A", 1)]))
    assert set(ctx) == {
        "type", "channel_count", "active_slot", "tg_active", "tg_cmd",
        "ch1_name", "ch1_id", "ch1_template", "ch1_active",
    }
    assert ctx["type"] == "panel_context"


def test_template_for_slot_returns_none_for_an_empty_tab(monkeypatch):
    """None means "keep showing what you were showing" -- better than
    switching the panel to an unrelated template because a tab was empty."""
    monkeypatch.setattr(pc, "_template_for_channel", lambda n: "")
    assert pc.template_for_slot(_Reader([_slot(1, "A", 1)]), 0) is None
    assert pc.template_for_slot(_Reader([_slot(1, "A", 1)]), 2) is None


def test_template_for_slot_resolves_the_selected_tab(monkeypatch):
    monkeypatch.setattr(pc, "_template_for_channel",
                        lambda n: {"A": "TA", "B": "TB"}.get(n, ""))
    reader = _Reader([_slot(1, "A", 1), _slot(2, "B", 2)])
    assert pc.template_for_slot(reader, 0) == "TA"
    assert pc.template_for_slot(reader, 1) == "TB"


def test_tg_cmd_lamp_reads_the_selected_channels_template(monkeypatch):
    """TG CMD is a per-template switch, so it has to follow the CH tab rather
    than describe the reader as a whole."""
    from forex_trader.core import core_ea_templates as _et
    monkeypatch.setattr(pc, "_template_for_channel",
                        lambda n: {"A": "TA", "B": "TB"}.get(n, ""))
    monkeypatch.setattr(_et, "get_ea_template",
                        lambda name: {"tg_cmd_enabled": name == "TB"})
    reader = _Reader([_slot(1, "A", 1), _slot(2, "B", 2)])
    assert pc.build_context(reader, active_slot=0)["tg_cmd"] == 0
    assert pc.build_context(reader, active_slot=1)["tg_cmd"] == 1
