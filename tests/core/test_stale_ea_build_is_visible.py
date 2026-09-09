"""A stale EA build must be visible on screen, not only in the log.

Reported live 2026-09-09. The owner recompiled the EA and re-attached it, and
the chart still showed the v1.05 behaviour -- Global Harvest reading "profit
per trade" and not summing across positions. `tools/deploy_ea.sh` had not been
run first, so MetaEditor rebuilt the copy already sitting in its own Experts
folder rather than the repo's v1.06.

**The app already knew.** It had logged, on every EA connection since 12:44:

    EA VERSION MISMATCH: terminal is running v1.05 (compiled 2026.09.09
    12:44:17) but this app ships EA source v1.06. The .ex5 is stale --
    run tools/deploy_ea.sh, then compile (F7).

It is a WARNING, so it sat in a log nobody was reading while the fix appeared
not to work. `deploy_ea.sh`'s own header records that this exact failure once
cost a full day: *"a batch of EA fixes was written, reviewed, committed, and
'recompiled' several times, while the terminal kept compiling a source from
three weeks earlier"*. It has now happened twice.

So the state reaches the top-bar EA badge, which is where an operator already
looks to see whether the EA is live. A green badge on a stale build is the
screen saying the opposite of the truth.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import ea_bridge


class _Bridge:
    def __init__(self, ok, running=None, expected=None):
        self.ea_version_ok = ok
        self.ea_version = running
        self.ea_source_version = expected


@pytest.fixture
def bridge(monkeypatch):
    def _set(b):
        monkeypatch.setattr(ea_bridge, "get_instance", lambda: b)
        return b
    return _set


class TestReadingTheBuildState:
    def test_a_stale_build_is_reported_stale(self, bridge):
        bridge(_Bridge(ok=False, running="1.05", expected="1.06"))

        stale, detail = ea_bridge.ea_build_status()

        assert stale is True
        assert "1.05" in detail and "1.06" in detail

    def test_the_detail_names_the_fix(self, bridge):
        """An operator seeing this must know what to do without reading the
        source -- the whole failure is that the instruction was only in a log."""
        bridge(_Bridge(ok=False, running="1.05", expected="1.06"))

        _stale, detail = ea_bridge.ea_build_status()

        assert "deploy_ea" in detail

    def test_a_matching_build_is_not_stale(self, bridge):
        bridge(_Bridge(ok=True, running="1.06", expected="1.06"))

        assert ea_bridge.ea_build_status()[0] is False

    def test_an_unchecked_build_is_not_reported_stale(self, bridge):
        """`ea_version_ok` is None when there is no EA source to compare
        against -- a packaged install. Unknown is not the same as stale, and
        crying wolf there would train the badge to be ignored."""
        bridge(_Bridge(ok=None))

        assert ea_bridge.ea_build_status()[0] is False

    def test_no_ea_connected_is_not_stale(self, monkeypatch):
        """The badge already shows "not connected" for that; this must not
        overwrite it with a different complaint."""
        monkeypatch.setattr(ea_bridge, "get_instance", lambda: None)

        assert ea_bridge.ea_build_status()[0] is False

    def test_a_bridge_that_throws_is_not_stale(self, monkeypatch):
        """Runs on the header's refresh tick. It must never raise into the UI,
        and must not invent a warning from a failure to read one."""
        def _boom():
            raise RuntimeError("gone")
        monkeypatch.setattr(ea_bridge, "get_instance", _boom)

        assert ea_bridge.ea_build_status()[0] is False


class TestItReachesTheScreenThroughTheController:
    def test_the_controller_exposes_it(self):
        """The frontend may not import services directly -- the
        frontend-reaches-the-backend-through-controllers contract is enforced
        at zero."""
        from backend.src.controllers import broker_controller

        assert hasattr(broker_controller, "ea_build_status")

    def test_the_header_asks_for_it(self):
        import inspect
        from frontend.app import _header

        assert "ea_build_status" in inspect.getsource(_header)

    def test_the_header_asks_for_the_badge_state_rather_than_deciding_itself(self):
        import inspect
        from frontend.app import _header

        assert "ea_badge_state" in inspect.getsource(_header)


class TestTheBadgeDecision:
    """Tested as a function, not as a grep.

    The first version of this asserted that "orange" appeared in the header's
    source near the staleness check. A mutation making the amber branch
    unreachable (`if False:`) left the string in place and passed -- so the
    decision was extracted and is asserted directly.
    """

    def test_stale_outranks_connected(self):
        colour, text, tip = ea_bridge.ea_badge_state(
            True, True, "local", "the detail")

        assert colour == "orange"
        assert "STALE" in text
        assert tip == "the detail"

    def test_connected_and_current_is_green(self):
        colour, text, _ = ea_bridge.ea_badge_state(True, False, "local", "")

        assert colour == "green"
        assert "STALE" not in text

    def test_not_connected_is_red(self):
        colour, _text, tip = ea_bridge.ea_badge_state(False, False, "VPS", "")

        assert colour == "red"
        assert "VPS" in tip

    def test_not_connected_beats_stale(self):
        """A disconnected EA is not running ANY build; complaining about which
        one would be noise on top of the real problem."""
        colour, _t, _ = ea_bridge.ea_badge_state(False, True, "local", "d")

        assert colour == "red"
