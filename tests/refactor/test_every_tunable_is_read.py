"""Every adaptive parameter must be read by something.

Three of them were not, and each did the same damage in its own way: the
nightly AI tuner is handed the catalogue as its menu, so a dead entry is a
lever it spends a decision on and believes it has pulled.

  * `allow_asian` (Bounce) — the tuner set it to 0 on 2026-07-24, reading as
    "stop trading the Asian session". The engine then produced 65 Asian
    signals, 38% of everything it has ever made. bugs/045.
  * `hour_filter_enabled` — the Breakout engine deleted its copy on
    2026-07-16 with the note *"the nightly AI tuner had set it to 0 believing
    it was disabling a filter"*. The Bounce engine still has one. bugs/048.
  * `daily_loss_stop_usd` — both catalogues describe a daily loss stop, each
    citing the day it would have prevented. Neither engine implements one.
    bugs/048.

A parameter's description is also read by a person, on the engine's own
parameters panel. A described protection that does not exist is worse than an
absent one, because it is believed.

**What this catches, and what it does not.** It catches a parameter with no
reader anywhere. It does NOT catch `allow_asian`, which is the one that
actually cost something: that has a reader — `session_quality` — but the
Bounce engine never calls that function, and the only thing that does asks it
one line before refusing the Asian session unconditionally. Finding that needs
a call graph, not a grep, and the grep should not be dressed up as more than it
is. A scanner that reports "all good" on ground it never walked is this repo's
founding cautionary tale; saying plainly where it stops is the guard against
becoming one.

This is a shrink-only ratchet, like the fixture and LOC gates: the known-dead
set may lose entries and must never gain one.
"""
from __future__ import annotations

import re

import pytest

from backend.src.services.breakout_signal import adaptive_params as bo_params
from backend.src.services.test_signal import adaptive_params as bounce_params
from tests.refactor._tunable_scan import CATALOGUE_FILES, readers_of

# Known dead, each with a bug tracking the decision. Shrink-only: when one is
# deleted or wired up, remove it here. Adding to this set is the regression
# this file exists to stop.
KNOWN_DEAD = {
    ("bounce", "hour_filter_enabled"),    # bugs/048
    ("bounce", "daily_loss_stop_usd"),    # bugs/048
    ("breakout", "daily_loss_stop_usd"),  # bugs/048
}

_CATALOGUES = {
    "bounce": bounce_params,
    "breakout": bo_params,
}


def _dead() -> set[tuple[str, str]]:
    out = set()
    for engine, module in _CATALOGUES.items():
        for name in module.PARAMS:
            if not readers_of(name, exclude=CATALOGUE_FILES):
                out.add((engine, name))
    return out


class TestNoNewDeadTunables:
    def test_every_parameter_is_read_somewhere(self):
        unexpected = _dead() - KNOWN_DEAD

        assert not unexpected, (
            f"adaptive parameters defined but read by nothing: {sorted(unexpected)} — "
            "the nightly tuner is handed this catalogue as its menu, so a dead entry "
            "is a decision it spends and believes it has applied. Wire it up or "
            "delete it; do not add it to KNOWN_DEAD."
        )

    def test_the_known_dead_set_has_no_slack(self):
        """A shrinking baseline with room in it is room to regress invisibly —
        and it would also leave this file describing a fix that had landed."""
        assert _dead() == KNOWN_DEAD


class TestTheScannerCanSee:
    """Negative controls. A scanner that answers "all good" whatever the tree
    looks like is this repo's founding cautionary tale."""

    def test_a_parameter_that_is_read_is_seen_as_read(self):
        assert readers_of("min_quality_score", exclude=CATALOGUE_FILES)

    def test_a_name_nothing_mentions_is_seen_as_dead(self):
        assert not readers_of("a_parameter_no_one_has_ever_written", exclude=CATALOGUE_FILES)

    def test_the_catalogues_themselves_are_excluded(self):
        """Otherwise every parameter is trivially "read" by its own
        definition, and this whole file passes vacuously."""
        assert readers_of("hour_filter_enabled", exclude=())
        assert not readers_of("hour_filter_enabled", exclude=CATALOGUE_FILES)


@pytest.mark.parametrize("engine", sorted(_CATALOGUES))
def test_each_catalogue_actually_has_parameters(engine):
    """If a catalogue were renamed or emptied, every test above would pass on
    nothing at all."""
    assert len(_CATALOGUES[engine].PARAMS) > 5
