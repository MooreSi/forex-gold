"""A `panel_data` module exists to serve a panel. If the panel goes, it goes.

Each engine has one `panel_data.py` whose `__all__` is the exact surface its
panel calls — every entry is "one named operation the panel performs". So an
entry nothing mentions is not a private helper left over from a refactor; it is
a read that lost its reader.

That happened. Bounce's panel was deleted on 2026-09-02 (`docs/todo/bugs/046`)
and three of its reads outlived it — `change_signature`, `param_specs` and
`ml_features_for_signal`.

**Closed 2026-09-14 by deleting the engine**, `panel_data.py` included, so the
known-dead set is now empty. Worth recording what went with it:
`change_signature` was *"a cheap comparable snapshot used to decide whether the
panel needs a re-render… it IS the diffing check"*. The Reversal and Breakout
panels still clear and rebuild six containers on a timer and still account for
about 30% of the app's event-loop stalls. A working diffing check existed, for
the wrong panel, and now does not exist at all. If `bugs/030` is picked up
again, that mechanism is in this file's git history rather than in the tree.

The two remaining engines are clean: 17 of 17 and 13 of 13 exported names are
referenced. This is a shrink-only ratchet — the known-dead set may lose entries
and must never gain one.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.refactor._source_scan import REPO, references_to

_MODULES = (
    "backend/src/services/breakout_signal/panel_data.py",
    "backend/src/services/reversal_engine/panel_data.py",
)

# Empty since 2026-09-14, and the point is to keep it that way. Remove an entry
# when it is wired up or deleted; never add one.
# Waiting for a caller the React port has not written yet — NOT dead.
#
# `reset_adaptive_params` was called by the Breakout engine panel, which the
# big-bang replace deleted on 2026-09-18 along with the other seven unported
# tabs. Same debt, same removal condition and the same refusal to call it dead
# as `AWAITING_REACT_PORT` in test_controller_operations_have_callers.py:
# shrink-only, and if task 080 lands with this entry still here, that is the
# evidence it was dead and the answer is a delete.
AWAITING_REACT_PORT: set[tuple[str, str]] = {
    ("breakout_signal", "reset_adaptive_params"),
}

KNOWN_DEAD: set[tuple[str, str]] = set()


def _engine_of(path: str) -> str:
    return path.split("/")[3]


def _dead() -> set[tuple[str, str]]:
    out = set()
    for path in _MODULES:
        mod = importlib.import_module(path[:-3].replace("/", "."))
        for name in getattr(mod, "__all__", []):
            if not references_to(name, exclude=(path,)):
                out.add((_engine_of(path), name))
    return out


class TestEveryPanelReadHasACaller:
    def test_no_new_orphaned_panel_reads(self):
        unexpected = _dead() - KNOWN_DEAD - AWAITING_REACT_PORT

        assert not unexpected, (
            f"panel_data exports nothing references: {sorted(unexpected)} — "
            "a panel read with no panel is a refactor leftover. Wire it up or "
            "delete it; do not add it to KNOWN_DEAD."
        )

    def test_the_known_dead_set_has_no_slack(self):
        """A shrinking baseline with room in it is room to regress invisibly,
        and would leave this file describing a clean-up that had happened."""
        assert _dead() == KNOWN_DEAD | AWAITING_REACT_PORT


class TestTheScannerCanSee:
    """Negative controls. A scanner that cannot fail is the thing this repo's
    rules were written after."""

    def test_a_name_that_is_used_is_seen_as_used(self):
        assert references_to("virtual_balance", exclude=())

    def test_a_name_nothing_mentions_is_seen_as_dead(self):
        assert not references_to("panel_read_that_does_not_exist", exclude=())

    def test_excluding_the_defining_file_is_what_makes_it_mean_anything(self):
        """Without the exclusion every name is trivially "referenced" by its
        own definition and this whole file passes on nothing.

        Phrased as "the defining file drops out" rather than "the name goes
        dead": every surviving export has a live caller, which is the property
        the gate is for, so there is no longer a name that goes to zero."""
        own = "backend/src/services/reversal_engine/panel_data.py"
        name = importlib.import_module(own[:-3].replace("/", ".")).__all__[0]
        assert own in references_to(name, exclude=())
        assert own not in references_to(name, exclude=(own,))


@pytest.mark.parametrize("path", _MODULES)
def test_each_module_actually_exports_something(path):
    mod = importlib.import_module(path[:-3].replace("/", "."))
    assert len(getattr(mod, "__all__", [])) > 5, path


def test_the_modules_listed_here_still_exist():
    """If one is renamed, the loop above would silently check fewer of them."""
    for path in _MODULES:
        assert (REPO / path).exists(), path
