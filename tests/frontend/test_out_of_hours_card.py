"""Out of Hours must be configurable from the app, not only from the database.

handover/020. `get_effective_strategy` is live -- `monitor_cycle.py:206` uses
it to choose which strategy manages a trade -- and every field it reads
(`ooh_enabled`, `ooh_start_time`, `ooh_end_time`, `ooh_strategy`, the holiday
date range, and now `ooh_timezone`) was settable only by editing the database
directly. The only thing on screen was a status label saying whether OOH was
currently active.

That is the mirror of the mistake made and reverted on 2026-09-07 in
bugs/024: there, a switch was added for a column nothing reads. Here, a live
money-path setting has no control at all. Both are ways for what the app does
and what the screen says to come apart, so this file pins the connection in
BOTH directions -- every field reaches the save call, and the save call writes
the keys `get_effective_strategy` actually reads.

Rendered detached rather than through tests/frontend/conftest's harness for
the reason recorded in the frontend domain file: that harness's fake reader
and stubbed controllers do not carry per-setting values, and a render test
through it would pass without proving a field is wired.
"""
from __future__ import annotations

import pytest
from nicegui import ui

from backend.src.controllers import settings_controller as settings_ctl
from backend.src.services.risk import risk_settings_repo as rsr
from frontend.pages.settings import _out_of_hours as ooh


def _walk(element):
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


@pytest.fixture
def saved(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(settings_ctl, "update_risk_settings",
                        lambda fields: calls.append(dict(fields)))
    monkeypatch.setattr(ui, "notify", lambda *a, **k: None)
    return calls


@pytest.fixture
def stored(monkeypatch):
    # Deliberately NOT the code's own defaults. With "22:00"/"07:00"/
    # "conservative" stored, a card that ignored the stored row and rendered
    # its defaults produced identical output and the test passed -- proved by
    # mutation, hardcoding the start time to "22:00" survived. A fixture whose
    # values match the defaults cannot tell the two apart.
    rs = {
        "ooh_enabled": 1, "ooh_start_time": "23:15", "ooh_end_time": "06:45",
        "ooh_strategy": "trail_stop", "ooh_timezone": "Europe/London",
        "ooh_date_active": 1, "ooh_date_from": "2026-12-24",
        "ooh_date_to": "2026-12-26",
    }
    monkeypatch.setattr(settings_ctl, "get_risk_settings", lambda: dict(rs))
    return rs


def _render():
    """Render inside an EXPLICIT client, not the ambient default slot.

    Rendering detached relies on the slot stack NiceGUI sets up at import, and
    `tests/frontend/conftest.py`'s `user_simulation` harness tears that down
    when it finishes. So a detached render passes when this file runs alone and
    raises "must be inside a slot" once any harness test has run first --
    order-dependent, and it cost a full green run to spot. An explicit Client
    owns its own slot and is unaffected by what ran before.
    """
    from nicegui.client import Client

    with Client(lambda: None, request=None):
        with ui.card() as root:
            ooh.render_out_of_hours_card()
    return root


def _texts(root):
    """Visible text AND the `label` prop.

    A ui.select's label lives in `_props`, not `.text` -- checking only `.text`
    reported every field as missing while the card rendered them correctly.
    """
    out = []
    for e in _walk(root):
        out.append(str(getattr(e, "text", "") or ""))
        out.append(str((getattr(e, "_props", {}) or {}).get("label", "") or ""))
    return " | ".join(out)


def _click_save(root):
    """Fire the Save button's real click handler.

    nicegui keeps handlers in `_event_listeners`, not a `handlers` list; the
    first version of this helper silently did nothing and every save assertion
    failed with an empty list rather than a wrong value.
    """
    for e in _walk(root):
        if isinstance(e, ui.button) and "save" in (e.text or "").lower():
            for listener in e._event_listeners.values():
                if listener.type == "click":
                    listener.handler(None)
                    return e
            raise AssertionError("the Save button has no click handler")
    raise AssertionError("no Save button on the card")


class TestTheCardIsThere:
    def test_it_renders_without_a_client(self, stored, saved):
        assert _render() is not None

    def test_it_names_itself(self, stored, saved):
        assert "Out of Hours" in _texts(_render())

    @pytest.mark.parametrize("label", ["Timezone", "Strategy"])
    def test_the_fields_are_labelled(self, stored, saved, label):
        assert label in _texts(_render())


class TestEveryFieldGetEffectiveStrategyReadsIsPresent:
    """The list is taken from `get_effective_strategy` itself, not from
    memory. A field it reads with no control is a setting that can only be
    wrong."""

    FIELDS = ("ooh_enabled", "ooh_start_time", "ooh_end_time",
              "ooh_strategy", "ooh_timezone", "ooh_date_active",
              "ooh_date_from", "ooh_date_to")

    def test_saving_writes_every_one_of_them(self, stored, saved):
        root = _render()

        _click_save(root)

        assert saved, "Save wrote nothing"
        written = saved[0]
        missing = [f for f in self.FIELDS if f not in written]
        assert not missing, f"Save never writes: {missing}"

    def test_the_stored_values_are_what_comes_back(self, stored, saved):
        """A card that renders defaults instead of what is stored would
        silently reset the window the first time anyone pressed Save."""
        root = _render()

        _click_save(root)
        written = saved[0]

        assert written["ooh_start_time"] == "23:15"
        assert written["ooh_end_time"] == "06:45"
        assert written["ooh_timezone"] == "Europe/London"
        assert written["ooh_strategy"] == "trail_stop"
        assert int(written["ooh_enabled"]) == 1
        assert int(written["ooh_date_active"]) == 1
        assert written["ooh_date_from"] == "2026-12-24"
        assert written["ooh_date_to"] == "2026-12-26"


class TestTheKeysMatchWhatTheEngineReads:
    def test_every_saved_key_is_one_the_resolver_uses(self, stored, saved):
        """Guards a typo: a key the card writes that nothing reads is the
        bugs/024 failure again, and it would look like it worked."""
        import inspect
        source = inspect.getsource(rsr.get_effective_strategy) + inspect.getsource(rsr._ooh_now)

        root = _render()
        _click_save(root)

        for key in saved[0]:
            assert key in source, f"{key} is written but never read"
