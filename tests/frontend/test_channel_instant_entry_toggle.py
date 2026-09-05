"""Every channel's Instant Market Entry flag must be visible and changeable.

docs/todo/bugs/024. `channel_parser_config.instant_entry_enabled` is one of
the two gates deciding whether a matched bare-direction trigger becomes a real
market order (`scan_messages.py`, `if bool(rs.get("immediate_market_entry", 0))
and ime_enabled`). It reached the frontend in exactly two places and both only
echoed it back unchanged, so the only thing that ever set it was a channel's
auto-bootstrap on first sight, and the only way to change it afterwards was a
direct database edit.

That is how GOLD DIGGERS INSTITUTIONAL came to match "PREPARE FOR A BUY"
exactly as designed, with the global gate open, and place nothing -- its own
flag was off, most likely left behind by the 2026-07-24 rename, since channel
config is keyed by channel_name and not by group_id.

The section is rendered detached rather than through tests/frontend/conftest's
harness because that harness's fake reader reports no slots, so the card it
draws is the "No channels loaded yet" empty state and no switch exists to
find. Detached rendering is the same code path, with the slots this card is
about.

The save is six POSITIONAL arguments with `instant_entry_enabled` and
`enabled` adjacent to each other, both booleans. Passing the new value one
slot out would disable the channel and leave the flag alone -- a swap that
type-checks, runs, and silently does the opposite of what was asked. So these
tests assert the exact call, not merely that a save happened.
"""
from __future__ import annotations

import pytest
from nicegui import ui

from backend.src.controllers import telegram_controller as tg_controller
from frontend.pages.telegram import _feed


class _Reader:
    def __init__(self, *names):
        self._slots = [
            {"slot": i, "group_name": n} for i, n in enumerate(names, start=1)
        ]

    def get_status(self, *a, **k):
        return {"slots": list(self._slots)}


def _walk(element):
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


@pytest.fixture
def saves(monkeypatch):
    """Records the save calls, and keeps notify out of a client-less render."""
    calls: list[tuple] = []
    monkeypatch.setattr(
        tg_controller, "save_channel_parser_config",
        lambda *a, **k: calls.append((a, k)),
    )
    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    return calls


@pytest.fixture
def config(monkeypatch):
    """One stored row per channel, mutable by the test before rendering."""
    rows: dict[str, dict] = {}
    monkeypatch.setattr(
        tg_controller, "get_channel_parser_config", lambda name: rows.get(name),
    )
    return rows


def _render(reader):
    with ui.card() as root:
        _feed._render_channels_active_section(reader)
    return root


def _switches(root, label: str):
    return [e for e in _walk(root)
            if isinstance(e, ui.switch) and e.text == label]


IME_LABEL = "Instant Entry"


class TestTheSwitchIsThere:
    def test_each_channel_gets_an_instant_entry_switch(self, saves, config):
        config["Alpha"] = {"instant_entry_enabled": 0, "enabled": 1}
        config["Beta"] = {"instant_entry_enabled": 1, "enabled": 1}

        root = _render(_Reader("Alpha", "Beta"))

        assert len(_switches(root, IME_LABEL)) == 2

    def test_the_switch_shows_what_is_stored(self, saves, config):
        """A control that always reads "off" is worse than none: it would have
        shown GOLD DIGGERS INSTITUTIONAL and Gold Diggers VIP identically,
        which is the confusion this whole bug is made of."""
        config["Off"] = {"instant_entry_enabled": 0, "enabled": 1}
        config["On"] = {"instant_entry_enabled": 1, "enabled": 1}

        root = _render(_Reader("Off", "On"))
        off, on = _switches(root, IME_LABEL)

        assert off.value is False
        assert on.value is True

    def test_a_channel_with_no_stored_row_reads_off(self, saves, config):
        """get_channel_parser_config returns None for a channel never saved.
        Off matches the auto-bootstrap default; guessing on would arm market
        entry for a channel nobody configured."""
        root = _render(_Reader("Unseen"))

        assert _switches(root, IME_LABEL)[0].value is False


class TestFlippingItSavesTheRightThing:
    def test_turning_it_on_saves_it_on(self, saves, config):
        config["Alpha"] = {
            "parser_format": "gd2", "signal_prefix": "p",
            "instant_entry_enabled": 0, "enabled": 1, "notes": "n",
        }
        root = _render(_Reader("Alpha"))

        _switches(root, IME_LABEL)[0].set_value(True)

        assert len(saves) == 1
        args, _ = saves[0]
        assert args == ("Alpha", "gd2", "p", True, True, "n")

    def test_turning_it_off_saves_it_off(self, saves, config):
        config["Alpha"] = {
            "parser_format": "gd2", "signal_prefix": "p",
            "instant_entry_enabled": 1, "enabled": 1, "notes": "n",
        }
        root = _render(_Reader("Alpha"))

        _switches(root, IME_LABEL)[0].set_value(False)

        args, _ = saves[0]
        assert args == ("Alpha", "gd2", "p", False, True, "n")

    def test_it_does_not_disturb_a_disabled_channel(self, saves, config):
        """The slot next to it. If the new value landed in `enabled` instead,
        this channel would come back on while its flag stayed off."""
        config["Alpha"] = {
            "parser_format": "auto", "signal_prefix": "",
            "instant_entry_enabled": 0, "enabled": 0, "notes": "",
        }
        root = _render(_Reader("Alpha"))

        _switches(root, IME_LABEL)[0].set_value(True)

        args, _ = saves[0]
        assert args[3] is True, "instant entry must be the value that changed"
        assert args[4] is False, "the channel must stay disabled"

    def test_each_channels_switch_saves_its_own_channel(self, saves, config):
        """Late binding in a loop: one closure per channel, or every switch
        writes the last one. This repo has a gate for that class of bug."""
        config["Alpha"] = {"instant_entry_enabled": 0, "enabled": 1}
        config["Beta"] = {"instant_entry_enabled": 0, "enabled": 1}
        root = _render(_Reader("Alpha", "Beta"))

        a, b = _switches(root, IME_LABEL)
        a.set_value(True)
        b.set_value(True)

        assert [args[0] for args, _ in saves] == ["Alpha", "Beta"]


class TestTheChannelSwitchStillWorks:
    """The existing enable/disable switch shares the same save call. It has no
    test of its own, and this change edits the call it makes."""

    def test_disabling_a_channel_preserves_its_instant_entry_flag(self, saves, config):
        config["Alpha"] = {
            "parser_format": "gd2", "signal_prefix": "",
            "instant_entry_enabled": 1, "enabled": 1, "notes": "",
        }
        root = _render(_Reader("Alpha"))

        _switches(root, "Alpha")[0].set_value(False)

        args, _ = saves[0]
        assert args == ("Alpha", "gd2", "", True, False, "")
