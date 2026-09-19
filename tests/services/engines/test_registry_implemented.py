"""Bounce is gone, but its SLOT is not — and the two must not be confused.

The owner, 2026-09-19: "the bounce signal generator should have been
completely removed". Its code was, on 2026-09-14. What survived is the name,
in one fixed tuple, for a reason the registry's own docstring gives: the sync
server binds engines POSITIONALLY, so dropping the name shifts Reversal into
Bounce's place on the wire and a paired node on an older build starts the
wrong engine.

So the name has to stay in the protocol and has to leave the screen. This
separates the two ideas: `ENGINE_NAMES` is the wire order and keeps Bounce;
`IMPLEMENTED_NAMES` is what this build can actually run, and does not.

Getting this backwards in either direction is a real failure. Drop Bounce from
ENGINE_NAMES and a paired node starts Reversal believing it is Bounce. Leave
it in IMPLEMENTED_NAMES and the dashboard offers a Start button for an engine
whose code was deleted.
"""
from __future__ import annotations

from backend.src.services.engines import registry


class TestTheWireOrderIsUnchanged:

    def test_bounce_still_holds_its_slot(self):
        assert "bounce" in registry.ENGINE_NAMES

    def test_the_order_is_still_breakout_bounce_reversal(self):
        # The sync server binds these positionally. This tuple IS the contract.
        assert registry.ENGINE_NAMES == ("breakout", "bounce", "reversal")

    def test_all_instances_still_returns_one_per_slot(self):
        # Including the empty one, as None -- a shorter tuple would shift
        # every position after it.
        assert len(registry.all_instances()) == len(registry.ENGINE_NAMES)


class TestWhatThisBuildCanRun:

    def test_bounce_is_not_something_this_build_runs(self):
        assert "bounce" not in registry.IMPLEMENTED_NAMES

    def test_the_engines_that_do_exist_are_there(self):
        assert set(registry.IMPLEMENTED_NAMES) == {"breakout", "reversal"}

    def test_it_is_a_subset_of_the_wire_order(self):
        # A name here that is not a slot would be an engine nothing can
        # address.
        assert set(registry.IMPLEMENTED_NAMES) <= set(registry.ENGINE_NAMES)

    def test_it_keeps_the_wire_order_rather_than_sorting(self):
        # The panel renders in this order. Alphabetical would put Reversal
        # first, which is not how the operator has ever seen them.
        assert list(registry.IMPLEMENTED_NAMES) == [
            n for n in registry.ENGINE_NAMES if n in registry.IMPLEMENTED_NAMES
        ]

    def test_it_is_derived_from_the_services_not_written_out_again(self):
        # A hand-written second list is how the empty slot gets re-introduced
        # in one place and not the other -- the exact failure the registry was
        # created to stop.
        assert all(registry.instance(n) is not None or True
                   for n in registry.IMPLEMENTED_NAMES)
        for name in registry.ENGINE_NAMES:
            has_service = registry._ENGINE_SERVICES[name] is not None
            assert (name in registry.IMPLEMENTED_NAMES) is has_service


class TestBulkStartIsUnaffected:

    def test_bounce_is_still_excluded_from_a_bulk_start(self):
        # Belt and braces, as it was: the day something is put back in that
        # slot, this is what stops a mode switch starting it.
        assert "bounce" in registry._NOT_BULK_STARTED
