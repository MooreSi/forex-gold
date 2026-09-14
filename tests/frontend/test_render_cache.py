"""A container is rebuilt when its data moved, and not when it did not.

`docs/todo/bugs/030`. Eighty-nine per cent of this app's event-loop stalls are
the dashboard refreshing itself, and thirty per cent of them are two files:
`reversal_panel/__init__.py` and `breakout_panel/__init__.py`. Both call
`.clear()` on six containers every thirty seconds and build each one again from
scratch, whether or not anything changed. The database reads are already off
the loop; what blocks is NiceGUI element creation, which cannot be threaded
because UI objects are not thread-safe. The only way down is to stop rebuilding.

**The failure mode this is built around** is stated in bugs/030: *"a signature
that omits a field leaves a trading screen showing a stale number, which is
worse than a stall the owner can see."* The mechanism that used to exist,
`test_signal/panel_data.change_signature`, hand-picked five fields. That is
precisely the shape that goes stale: add a column to the table, forget the
signature, and the panel silently stops showing it.

So this one cannot omit a field, by construction. The digest is taken over the
**whole payload the container renders from**, not a chosen subset. If the
payload is equal the rendered output is equal, because the render is a pure
function of the payload and nothing else. That is a property a test can hold,
and the tests below hold it:

  * every field participates -- checked by mutating each key in turn;
  * ordering is significant, because the render draws rows in order;
  * a float that reads equal IS equal, and `0` and `False` and `None` are three
    different things, because a payload that conflates them would skip a real
    change;
  * the digest never raises, whatever is in the payload -- a diffing check that
    can throw is worse than no diffing check, since it takes the panel with it.

**And it is off by default.** bugs/030 says this change wants the owner watching
the panel while it is switched on, not an overnight commit. `SectionCache` in
its default state answers "yes, rebuild" every single time, which is today's
behaviour exactly.
"""
from __future__ import annotations

import math

import pytest

from frontend.components.render_cache import SectionCache, payload_digest


# ── the digest ───────────────────────────────────────────────────────────────

class TestEveryFieldParticipates:
    """The negative control for the whole design. If one key could change
    without moving the digest, this is a stale-number machine."""

    _ROW = {"id": 7, "status": "open", "stop_loss": 2390.5, "ml_prob": 0.61,
            "sl_moved_to_be": False, "note": None, "tags": ["a", "b"]}

    @pytest.mark.parametrize("key", sorted(_ROW))
    def test_changing_any_single_field_changes_the_digest(self, key):
        other = dict(self._ROW)
        other[key] = "MUTATED-SENTINEL"
        assert payload_digest([self._ROW]) != payload_digest([other]), key

    def test_an_identical_payload_gives_an_identical_digest(self):
        assert payload_digest([self._ROW]) == payload_digest([dict(self._ROW)])

    def test_a_new_key_appearing_changes_it(self):
        """A column added to the query must not slip past silently -- that is
        the exact way the old hand-picked signature would have gone stale."""
        extra = dict(self._ROW, newly_added_column=1)
        assert payload_digest([self._ROW]) != payload_digest([extra])

    def test_a_key_disappearing_changes_it(self):
        fewer = {k: v for k, v in self._ROW.items() if k != "note"}
        assert payload_digest([self._ROW]) != payload_digest([fewer])


class TestAContainerIsNotAScalarAndAMapIsNotAList:
    """Found by mutation-testing: tagging mappings and sequences alike made an
    empty dict and an empty list digest the same. "No rows" arriving as `{}`
    where `[]` was expected is a different shape from the repo, and the panel
    should redraw rather than assume."""

    def test_an_empty_mapping_is_not_an_empty_sequence(self):
        assert payload_digest({}) != payload_digest([])

    def test_a_mapping_is_not_the_sequence_of_its_items(self):
        assert payload_digest({"a": 1}) != payload_digest([("a", 1)])

    def test_an_empty_set_is_not_an_empty_sequence_either(self):
        """Same mutant, the other container type. Sets do not appear in any
        panel payload today; the tag that keeps them apart is one character
        and this is what makes it mean something."""
        assert payload_digest(set()) != payload_digest([])


class TestOrderingIsSignificant:
    """Rows are drawn in the order they arrive, so a reordering IS a change."""

    def test_two_rows_swapped_is_a_different_digest(self):
        a, b = {"id": 1}, {"id": 2}
        assert payload_digest([a, b]) != payload_digest([b, a])

    def test_dict_key_order_is_NOT_significant(self):
        """The opposite case: the same mapping built in a different order is
        the same data, and rebuilding on it would defeat the whole point."""
        assert (payload_digest({"a": 1, "b": 2})
                == payload_digest({"b": 2, "a": 1}))


class TestValuesThatLookAlikeAreNotTreatedAsAlike:

    @pytest.mark.parametrize("pair", [
        (0, False), (1, True), (0, None), (False, None), (0, "0"), (0, 0.0),
    ])
    def test_these_are_all_distinguished(self, pair):
        first, second = pair
        assert payload_digest(first) != payload_digest(second), pair

    def test_a_price_that_moved_one_hundredth_is_a_change(self):
        """Stops and levels are rendered to two decimals. 2390.50 against
        2390.51 is a different number on screen."""
        assert payload_digest(2390.50) != payload_digest(2390.51)

    def test_a_price_that_did_not_move_is_not(self):
        assert payload_digest(2390.50) == payload_digest(2390.5)

    def test_negative_zero_is_not_the_same_as_zero(self):
        """Found by mutation-testing this module's own first draft, which
        folded them together as "obviously the same number". They are not the
        same on screen: `_pnl_str` renders with an explicit sign, so -0.0 is
        "-0.00" and 0.0 is "+0.00". Folding them leaves the wrong sign showing
        -- the precise failure class this file exists to prevent, written into
        the guard against it."""
        assert payload_digest(-0.0) != payload_digest(0.0)


class TestItNeverRaises:
    """A diffing check that throws takes the panel down with it. Every one of
    these is something a repo row or an engine cache has actually held."""

    @pytest.mark.parametrize("payload", [
        None, [], {}, "", 0, float("nan"), float("inf"), -0.0,
        {"nested": {"deep": [1, {"deeper": (2, 3)}]}},
        [{"ts": 1789379262.3572521}],
        {"bytes": b"\x00\xff"},
        {"set": {3, 1, 2}},
        [object()],
        {"self_describing": type("X", (), {})()},
    ])
    def test_it_returns_a_string(self, payload):
        assert isinstance(payload_digest(payload), str)

    def test_nan_is_stable_against_itself(self):
        """NaN != NaN in Python. If that leaked through, a container holding
        one would rebuild on every single tick -- the bug, not the fix."""
        assert payload_digest(float("nan")) == payload_digest(float("nan"))

    def test_an_unhashable_unserialisable_object_still_answers(self):
        assert isinstance(payload_digest(_Awkward()), str)

    def test_one_unreadable_field_does_not_destabilise_the_whole_row(self):
        """The mutant this kills let the failure escape `_canonical` and be
        caught around the whole digest instead. That answers, but with a value
        keyed on the payload object's identity -- so two equal payloads digest
        differently and the container rebuilds on every single tick. Degrading
        to "always rebuild" for one field is fine; doing it for the row is the
        bug this module exists to remove."""
        awkward = _Awkward()

        assert (payload_digest([{"id": 1, "obj": awkward}])
                == payload_digest([{"id": 1, "obj": awkward}]))

    def test_and_its_siblings_are_still_seen(self):
        awkward = _Awkward()

        assert (payload_digest([{"id": 1, "obj": awkward}])
                != payload_digest([{"id": 2, "obj": awkward}]))


class _Awkward:
    """A value that cannot be rendered as text at all. Contrived, and the
    cheapest way to drive the `_canonical` fallback deliberately."""

    def __repr__(self):
        raise RuntimeError("even repr fails")


# ── the cache ────────────────────────────────────────────────────────────────

class TestTheDefaultIsTodaysBehaviour:
    """bugs/030: this wants the owner watching the panel while it is switched
    on. Until he does, every call must answer "rebuild"."""

    def test_a_default_cache_always_says_rebuild(self):
        c = SectionCache()
        assert c.changed("levels", [{"price": 1}]) is True
        assert c.changed("levels", [{"price": 1}]) is True
        assert c.changed("levels", [{"price": 1}]) is True

    def test_it_is_disabled_unless_explicitly_enabled(self):
        assert SectionCache().enabled is False


class TestWhenEnabled:

    @pytest.fixture
    def cache(self):
        return SectionCache(enabled=True)

    def test_the_first_call_always_rebuilds(self, cache):
        assert cache.changed("levels", [{"price": 1}]) is True

    def test_an_unchanged_payload_skips_the_rebuild(self, cache):
        cache.changed("levels", [{"price": 1}])

        assert cache.changed("levels", [{"price": 1}]) is False

    def test_a_changed_payload_rebuilds(self, cache):
        cache.changed("levels", [{"price": 1}])

        assert cache.changed("levels", [{"price": 2}]) is True

    def test_and_then_settles_again(self, cache):
        cache.changed("levels", [{"price": 1}])
        cache.changed("levels", [{"price": 2}])

        assert cache.changed("levels", [{"price": 2}]) is False

    def test_sections_are_independent(self, cache):
        """Six containers share one cache. One moving must not mark the other
        five as fresh, nor force them to rebuild."""
        cache.changed("levels", [{"price": 1}])
        cache.changed("open", [{"id": 1}])

        assert cache.changed("levels", [{"price": 2}]) is True
        assert cache.changed("open", [{"id": 1}]) is False

    def test_an_empty_payload_is_a_real_state(self, cache):
        """The last open signal closing takes the list to empty. That is a
        change, and the "No open signals" line has to replace the rows."""
        cache.changed("open", [{"id": 1}])

        assert cache.changed("open", []) is True

    def test_and_staying_empty_is_not(self, cache):
        cache.changed("open", [])

        assert cache.changed("open", []) is False


class TestADigestFailureFallsTowardsRebuilding:
    """`payload_digest` is total against every payload the tests above throw
    at it. This is the belt: the payload is whatever a repo handed the panel,
    and an exception escaping `changed` would take the five containers behind
    it down too."""

    @pytest.fixture
    def exploding(self, monkeypatch):
        import frontend.components.render_cache as rc
        monkeypatch.setattr(rc, "payload_digest",
                            lambda p: (_ for _ in ()).throw(ValueError("boom")))

    def test_it_rebuilds_rather_than_raising(self, exploding):
        assert SectionCache(enabled=True).changed("levels", [{"price": 1}]) is True

    def test_it_does_not_leave_a_stale_digest_behind(self, monkeypatch):
        """A section that failed to digest must not keep the digest from
        before it failed, or it stays frozen once digesting recovers -- the
        stale-screen failure this whole module is built to avoid.

        Deliberately does NOT use the `exploding` fixture: the real function
        has to be captured before anything is patched. The first draft of this
        test took its "real" reference after the fixture had already run, so
        it restored the exploding stub and passed against a cache that never
        forgot anything."""
        import frontend.components.render_cache as rc
        real = rc.payload_digest
        boom = lambda p: (_ for _ in ()).throw(ValueError("boom"))  # noqa: E731
        cache = SectionCache(enabled=True)

        cache.changed("levels", [{"price": 1}])          # a good digest stored
        monkeypatch.setattr(rc, "payload_digest", boom)
        cache.changed("levels", [{"price": 1}])          # fails, must forget
        monkeypatch.setattr(rc, "payload_digest", real)

        assert cache.changed("levels", [{"price": 1}]) is True


class TestForget:
    """A failed render must not leave the cache claiming that section is
    drawn. Every render site is inside a try/except that logs and carries on;
    if the digest were stored before the render, one exception would freeze
    that container until the payload moved again."""

    def test_forgetting_a_section_forces_the_next_rebuild(self):
        c = SectionCache(enabled=True)
        c.changed("levels", [{"price": 1}])

        c.forget("levels")

        assert c.changed("levels", [{"price": 1}]) is True

    def test_forgetting_an_unknown_section_is_not_an_error(self):
        SectionCache(enabled=True).forget("never-seen")

    def test_forget_all_clears_every_section(self):
        c = SectionCache(enabled=True)
        c.changed("a", [1])
        c.changed("b", [2])

        c.forget_all()

        assert c.changed("a", [1]) is True
        assert c.changed("b", [2]) is True


# ── the switch ───────────────────────────────────────────────────────────────

class TestTheSwitchDefaultsOff:
    """bugs/030 asks for this to be turned on with the owner watching the
    panel, because the failure mode is a stale number on a trading screen
    rather than an error. Everything below is about it staying off until then.
    """

    @pytest.fixture
    def stored(self, monkeypatch):
        """Stand in for app_config, without a database.

        Patches the FUNCTION, not `sys.modules`. The first version of this
        fixture replaced the module in `sys.modules`, which passed on its own
        and failed in the full suite: `from package import submodule` reads the
        attribute already set on the package once that submodule has been
        imported by anything else, and never consults `sys.modules` again."""
        from backend.src.controllers import settings_controller
        box: dict = {}
        monkeypatch.setattr(settings_controller, "get_app_config", box.get)
        return box

    def test_unset_is_off(self, stored):
        from frontend.components.render_cache import diff_rendering_enabled
        assert diff_rendering_enabled() is False

    def test_zero_is_off(self, stored):
        from frontend.components.render_cache import (DIFF_RENDERING_KEY,
                                                      diff_rendering_enabled)
        stored[DIFF_RENDERING_KEY] = "0"
        assert diff_rendering_enabled() is False

    def test_one_is_on(self, stored):
        from frontend.components.render_cache import (DIFF_RENDERING_KEY,
                                                      diff_rendering_enabled)
        stored[DIFF_RENDERING_KEY] = "1"
        assert diff_rendering_enabled() is True

    def test_anything_else_is_off(self, stored):
        """Not truthiness: only the exact stored "1" counts. A half-written
        value must not switch a panel's rendering."""
        from frontend.components.render_cache import (DIFF_RENDERING_KEY,
                                                      diff_rendering_enabled)
        for junk in ("true", "yes", "2", "", "on", " 1"):
            stored[DIFF_RENDERING_KEY] = junk
            assert diff_rendering_enabled() is False, junk

    def test_a_settings_read_that_raises_is_off_not_an_exception(self, monkeypatch):
        """Read at page build. A page that will not render because a
        performance preference could not be read is worse than the preference
        being ignored."""
        import frontend.components.render_cache as rc
        from backend.src.controllers import settings_controller

        def _boom(key):
            raise RuntimeError("no database")

        monkeypatch.setattr(settings_controller, "get_app_config", _boom)
        assert rc.diff_rendering_enabled() is False


class TestBothPanelsReadTheSameSwitch:
    """Two panels, one preference. A second key would make the switch mean
    different things on the two tabs of one page."""

    @pytest.mark.parametrize("rel", [
        "frontend/pages/reversal_panel/__init__.py",
        "frontend/pages/breakout_panel/__init__.py",
    ])
    def test_the_panel_builds_its_cache_from_the_shared_reader(self, rel):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2] / rel).read_text(
            encoding="utf-8")
        assert "SectionCache(enabled=diff_rendering_enabled())" in src, rel

    def test_the_toggle_writes_the_key_the_reader_reads(self):
        import pathlib
        src = (pathlib.Path(__file__).resolve().parents[2]
               / "frontend/pages/test_panel/__init__.py").read_text(encoding="utf-8")
        assert "set_app_config(DIFF_RENDERING_KEY," in src
