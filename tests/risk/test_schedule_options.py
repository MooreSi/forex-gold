"""What the Schedule screen needs besides the grid itself.

The React schedule screen was a grid of start/end boxes. The NiceGUI one it
replaced also carried the Trading Markets toggles, a per-window profit
target, and a per-source Override dropdown for every Telegram channel and
both internal engines. None of that had anywhere to come from over HTTP.

This module is what supplies it. It touches no broker: markets read and
write risk settings, and the override list is assembled from the strategy
catalogue and the saved EA templates.

`is_session_allowed` is reused rather than reimplemented. The NiceGUI page
computed its own session label from `datetime.utcnow().hour`, which is a
second opinion about the one question the gate already answers -- and the
two could disagree, leaving the badge saying London while the gate refused
the trade.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import schedule_options as so


@pytest.fixture
def lab(monkeypatch):
    state = {
        "rs": {"session_asia_enabled": 1, "session_london_enabled": 1,
               "session_ny_enabled": 0},
        "allowed": (True, "london"),
        "updates": [],
        "templates": [{"name": "Grid-A"}, {"name": "Runner"}],
        "channels": ["GoldSignals", "GD2"],
    }

    monkeypatch.setattr(so, "_get_risk_settings", lambda: dict(state["rs"]))
    monkeypatch.setattr(so, "_update_risk_settings",
                        lambda fields: state["updates"].append(dict(fields)))
    monkeypatch.setattr(so, "_is_session_allowed", lambda rs=None: state["allowed"])
    monkeypatch.setattr(so, "_list_ea_templates", lambda: list(state["templates"]))
    monkeypatch.setattr(so, "_override_for_template", lambda name: f"template:{name}")
    monkeypatch.setattr(so, "_channel_names", lambda: list(state["channels"]))
    return state


# ── Trading Markets ──────────────────────────────────────────────────────────

def test_the_three_market_toggles_are_reported(lab):
    out = so.markets()

    assert out["asia"] is True
    assert out["london"] is True
    assert out["new_york"] is False


def test_an_unset_toggle_defaults_to_enabled(lab):
    """A settings row saved before these keys existed must keep trading
    exactly as it did, not go dark."""
    lab["rs"] = {}

    out = so.markets()

    assert (out["asia"], out["london"], out["new_york"]) == (True, True, True)


def test_the_live_session_comes_from_the_gate_not_a_second_clock(lab):
    lab["allowed"] = (False, "asian")

    out = so.markets()

    assert out["session"] == "asian"
    assert out["allowed_now"] is False


def test_the_weekend_reports_closed(lab):
    lab["allowed"] = (False, "closed")

    assert so.markets()["session"] == "closed"


class TestWritingTheToggles:

    def test_one_market_can_be_switched_without_touching_the_others(self, lab):
        so.set_markets({"london": False})

        assert lab["updates"] == [{"session_london_enabled": 0}]

    def test_every_market_can_be_written_at_once(self, lab):
        so.set_markets({"asia": True, "london": False, "new_york": True})

        assert lab["updates"] == [{
            "session_asia_enabled": 1,
            "session_london_enabled": 0,
            "session_ny_enabled": 1,
        }]

    def test_an_unknown_key_is_refused_by_name(self, lab):
        """A typo that silently wrote nothing would look exactly like a
        toggle that does not work."""
        with pytest.raises(ValueError, match="tokyo"):
            so.set_markets({"tokyo": True})

        assert lab["updates"] == []

    def test_nothing_to_write_writes_nothing(self, lab):
        so.set_markets({})

        assert lab["updates"] == []


# ── The Override dropdown ────────────────────────────────────────────────────

class TestTheOverrideOptions:

    def test_no_override_is_first_and_is_the_empty_value(self, lab):
        options = so.override_options()

        assert options[0] == {"value": "", "label": "— No Override —"}

    def test_auto_sits_directly_under_it(self, lab):
        """Second by intent, not by accident: it is the option most likely
        to be wanted, which is why the NiceGUI page put it there."""
        assert so.override_options()[1]["value"] == "auto"

    def test_every_saved_ea_template_is_offered(self, lab):
        values = [o["value"] for o in so.override_options()]

        assert "template:Grid-A" in values
        assert "template:Runner" in values

    def test_a_template_is_labelled_as_one(self, lab):
        labels = {o["value"]: o["label"] for o in so.override_options()}

        assert labels["template:Grid-A"] == "Template: Grid-A"

    def test_the_built_in_strategies_are_offered(self, lab):
        from backend.src.utils.models import STRATEGY_NAMES

        values = [o["value"] for o in so.override_options()]

        for key in STRATEGY_NAMES:
            assert key in values

    def test_an_install_with_no_templates_still_offers_the_strategies(self, lab):
        lab["templates"] = []

        values = [o["value"] for o in so.override_options()]

        assert values[0] == ""
        assert len(values) > 2

    def test_a_broken_template_store_does_not_empty_the_dropdown(self, lab,
                                                                monkeypatch):
        """Losing the templates must cost the template entries, not the
        ability to pick a strategy at all."""
        def _boom():
            raise RuntimeError("templates table missing")

        monkeypatch.setattr(so, "_list_ea_templates", _boom)

        values = [o["value"] for o in so.override_options()]

        assert values[0] == ""
        assert "auto" in values


# ── The channels a window can gate ───────────────────────────────────────────

def test_the_telegram_channels_are_listed(lab):
    assert so.channel_names() == ["GoldSignals", "GD2"]


def test_a_broken_channel_store_reports_none_rather_than_raising(lab, monkeypatch):
    def _boom():
        raise RuntimeError("telegram store offline")

    monkeypatch.setattr(so, "_channel_names", _boom)

    assert so.channel_names() == []


# ── Against the real collaborators ───────────────────────────────────────────
# Everything above replaces them, which is what makes those tests fast and
# what makes them blind: the first version of this module imported
# `services.telegram.channels`, a module that does not exist, and every test
# above passed anyway because they all patched over it. These do not patch.

class TestTheRealWiring:

    def test_the_channel_lookup_reaches_a_module_that_exists(self, fresh_db):
        """The bug the stubs hid, and the reason this calls the PRIVATE
        wrapper.

        The first version of this module imported `services.telegram.channels`,
        which does not exist. Every stubbed test passed, and so did the first
        version of THIS test -- because `channel_names()` catches the
        ImportError and returns `[]`, which is indistinguishable from an
        install with no channels. Asserting the public function returns a list
        is therefore a tautology.

        `_channel_names()` is the unguarded import. If the path is wrong it
        raises here, which is the only place it can be seen.
        """
        assert isinstance(so._channel_names(), list)

    def test_the_markets_read_reaches_the_real_settings(self, fresh_db):
        out = so.markets()

        assert set(out) == {"asia", "london", "new_york", "session", "allowed_now"}
        assert isinstance(out["session"], str)

    def test_a_market_written_here_is_read_back_by_the_gate(self, fresh_db):
        """The round trip, through the real settings table: the names this
        module speaks and the keys the gate reads have to line up."""
        so.set_markets({"london": False})

        assert so.markets()["london"] is False

        so.set_markets({"london": True})

        assert so.markets()["london"] is True

    def test_the_override_options_build_from_the_real_catalogue(self, fresh_db):
        options = so.override_options()

        assert options[0]["value"] == ""
        assert options[1]["value"] == "auto"
        assert len(options) > 2


# ── The screen's extras, assembled ───────────────────────────────────────────

class TestScreenExtras:
    """`/api/schedule/state` is the screen's ONE read. The grid, the enabled
    flag and the daily target are the screen; the markets card, the override
    list and the channel list are additions to it. An addition that cannot be
    read must cost itself, not the page -- a 500 here leaves the operator
    unable to see or edit the windows because a dropdown's contents were
    unavailable."""

    def test_it_carries_all_three(self, lab):
        out = so.screen_extras()

        assert set(out) == {"markets", "override_options", "channels"}
        assert out["markets"]["london"] is True
        assert out["channels"] == ["GoldSignals", "GD2"]

    def test_an_unreadable_markets_card_reports_null_not_all_enabled(self, lab,
                                                                    monkeypatch):
        """The one fallback that must not be cheerful. Defaulting to "every
        market on" would tell the operator trading is allowed in sessions
        that may be switched off."""
        def _boom():
            raise RuntimeError("no settings table")

        monkeypatch.setattr(so, "_get_risk_settings", _boom)

        out = so.screen_extras()

        assert out["markets"] is None

    def test_an_unreadable_override_list_still_leaves_the_grid_usable(self, lab,
                                                                     monkeypatch):
        def _boom():
            raise RuntimeError("catalogue gone")

        monkeypatch.setattr(so, "override_options", _boom)

        out = so.screen_extras()

        assert out["override_options"] == []
        assert out["markets"]["london"] is True
