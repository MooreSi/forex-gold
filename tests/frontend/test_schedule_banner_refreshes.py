"""The "Schedule Override" banner must follow the setting, not the page load.

bugs/032. The owner turned the Trading Schedule off and Trading > Strategy
carried on showing the amber Schedule Override banner. Verified against the
live database at the time: `is_trading_schedule_enabled()` was False and
`app_config['trading_schedule_enabled']` was '0' in both demo databases, and
`get_app_config` is not cached — the flag really was off.

`_strategy_cards.py` evaluated it once while rendering and drew the row, or
did not, there and then. Its own comment said so: "this renders once on page
load". Changing the setting on the Schedule tab left the drawn banner in place
until a browser reload.

The card already polls every 60 seconds (`ui.timer(60,
_refresh_tooltips_from_db)`) to keep its recommendation tooltips current. The
banner simply sat outside that. It is display-only — the gate itself reads the
flag fresh on every signal — but it is one of the screens an operator checks
*before* deciding whether a trade was routed as expected, so a stale one says
the opposite of the truth at exactly the wrong moment.

Rendered under an explicit `Client` rather than the ambient slot, for the
reason recorded in the frontend domain file: the shared render harness tears
the default slot stack down when it finishes, so a detached render passes
alone and fails once any harness test has run first.
"""
from __future__ import annotations

import pytest
from nicegui import ui

from frontend.pages.trading import _strategy_cards as sc


def _walk(element):
    yield element
    for slot in element.slots.values():
        for child in slot.children:
            yield from _walk(child)


def _render_banner(enabled: bool):
    from nicegui.client import Client

    with Client(lambda: None, request=None):
        with ui.card() as root:
            row = sc.render_schedule_override_banner(lambda: enabled)
    return root, row


def _banner_visible(root) -> bool:
    for e in _walk(root):
        if "bg-amber-900" in " ".join(e._classes or []):
            return e.visible
    return False


class TestItReflectsTheSettingWhenDrawn:
    def test_on_means_visible(self):
        root, _ = _render_banner(True)

        assert _banner_visible(root) is True

    def test_off_means_not_visible(self):
        root, _ = _render_banner(False)

        assert _banner_visible(root) is False

    def test_the_row_exists_either_way(self):
        """It is built once and hidden, not conditionally created — otherwise
        there is nothing for the refresh to turn back on."""
        root, row = _render_banner(False)

        assert row is not None
        assert any("bg-amber-900" in " ".join(e._classes or []) for e in _walk(root))


class TestItFollowsTheSettingAfterwards:
    """The actual bug: the setting changed and the banner did not."""

    def test_turning_it_off_hides_an_already_drawn_banner(self):
        flag = {"on": True}
        from nicegui.client import Client

        with Client(lambda: None, request=None):
            with ui.card() as root:
                sc.render_schedule_override_banner(lambda: flag["on"])
        assert _banner_visible(root) is True

        flag["on"] = False
        sc.refresh_schedule_override_banner()

        assert _banner_visible(root) is False, "the banner ignored the change"

    def test_turning_it_on_shows_it(self):
        flag = {"on": False}
        from nicegui.client import Client

        with Client(lambda: None, request=None):
            with ui.card() as root:
                sc.render_schedule_override_banner(lambda: flag["on"])

        flag["on"] = True
        sc.refresh_schedule_override_banner()

        assert _banner_visible(root) is True

    def test_a_reader_that_throws_leaves_the_banner_alone(self):
        """This runs on a 60s timer in the UI. A database hiccup must not
        blank a safety-relevant banner, nor raise into the timer."""
        def _boom():
            raise RuntimeError("db gone")
        from nicegui.client import Client

        with Client(lambda: None, request=None):
            with ui.card() as root:
                sc.render_schedule_override_banner(lambda: True)

        sc.refresh_schedule_override_banner(_reader=_boom)

        assert _banner_visible(root) is True
