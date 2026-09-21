"""What to do when two sources disagree about direction.

A pure decision function: candidate + what is already live + a policy, in;
a verdict, out. No database, no broker, no clock beyond the `now` it is
handed, so every branch can be stated as a sentence and asserted.

The expected behaviour comes from the design, not from the code:

  * A source never contradicts ITSELF here. A channel that flips direction
    is issuing a correction (scan_edit_reparse.py already owns that case,
    and it can close a real position); an engine holding both sides is a
    position-level question `core_internal_exposure_guard` already answers,
    with measured evidence that blocking it costs most of the profit.
  * Cancelling a signal that has not filled is free. Closing one that has
    is the frozen close path. No verdict may ever imply the second.
  * The policy names which SOURCE-KIND PAIRS it governs. Engine-vs-engine,
    channel-vs-engine and channel-vs-channel are three different questions
    and one global switch would get at least one of them wrong.
  * An unavailable fact abstains. The bus does not record fill state yet,
    so the one mode that needs it returns UNKNOWN rather than guessing.
  * A verdict is advisory. Nothing here is wired to execution in stage 1.
"""
from __future__ import annotations

import pytest

from backend.src.services.signals import contradiction as c


NOW = 1_700_000_000.0


def cand(direction="BUY", kind=c.KIND_TELEGRAM, name="Gold Diggers VIP", **over):
    return c.Candidate(source_kind=kind, source_name=name, direction=direction,
                       symbol=over.pop("symbol", "XAUUSD"), at=over.pop("at", NOW))


def active(direction="SELL", kind=c.KIND_ENGINE, name="Reversal Engine", **over):
    return c.Active(
        source_kind=kind, source_name=name, direction=direction,
        symbol=over.pop("symbol", "XAUUSD"),
        created_at=over.pop("created_at", NOW - 60.0),
        is_pending=over.pop("is_pending", True),
        signal_ref=over.pop("signal_ref", "sig-1"),
    )


FIRST_WINS = c.Policy("first wins", c.MODE_FIRST_WINS, window_secs=600.0)
FRESHEST   = c.Policy("freshest wins", c.MODE_FRESHEST_WINS, window_secs=600.0)
SHRINK     = c.Policy("half size", c.MODE_SHRINK, window_secs=600.0, lot_mult=0.5)


class TestWhenThereIsNothingToArgueWith:
    def test_an_empty_bus_allows(self):
        v = c.resolve(cand(), [], FIRST_WINS, now=NOW)
        assert v.action == c.ALLOW

    def test_a_signal_in_the_SAME_direction_is_not_a_contradiction(self):
        v = c.resolve(cand("BUY"), [active("BUY")], FIRST_WINS, now=NOW)
        assert v.action == c.ALLOW

    def test_the_off_policy_allows_even_a_head_on_disagreement(self):
        """The champion. It is what the live path does today, and a study
        without it has nothing to compare against."""
        off = c.Policy("live (champion)", c.MODE_OFF)
        v = c.resolve(cand("BUY"), [active("SELL")], off, now=NOW)
        assert v.action == c.ALLOW


class TestWhatCountsAsOpposing:
    def test_an_opposite_direction_from_another_source_does(self):
        v = c.resolve(cand("BUY"), [active("SELL")], FIRST_WINS, now=NOW)
        assert v.action == c.BLOCK
        assert "Reversal Engine" in v.reason

    def test_the_SAME_source_disagreeing_with_itself_does_not(self):
        """An edit that flips direction is a correction, not a fight. The
        scan path already handles it, and treating it as a contradiction
        would block every corrected signal."""
        v = c.resolve(cand("BUY", name="Gold Diggers VIP"),
                      [active("SELL", kind=c.KIND_TELEGRAM, name="Gold Diggers VIP")],
                      FIRST_WINS, now=NOW)
        assert v.action == c.ALLOW

    def test_matching_on_source_name_ignores_case_and_padding(self):
        v = c.resolve(cand("BUY", name="Gold Diggers VIP"),
                      [active("SELL", kind=c.KIND_TELEGRAM, name=" gold diggers vip ")],
                      FIRST_WINS, now=NOW)
        assert v.action == c.ALLOW

    def test_another_instrument_does_not(self):
        v = c.resolve(cand("BUY", symbol="XAUUSD"),
                      [active("SELL", symbol="EURUSD")], FIRST_WINS, now=NOW)
        assert v.action == c.ALLOW

    def test_an_unknown_instrument_DOES_because_it_cannot_be_ruled_out(self):
        v = c.resolve(cand("BUY", symbol="XAUUSD"),
                      [active("SELL", symbol="")], FIRST_WINS, now=NOW)
        assert v.action == c.BLOCK

    def test_something_older_than_the_window_does_not(self):
        v = c.resolve(cand("BUY"), [active("SELL", created_at=NOW - 601.0)],
                      FIRST_WINS, now=NOW)
        assert v.action == c.ALLOW

    def test_something_inside_the_window_does(self):
        v = c.resolve(cand("BUY"), [active("SELL", created_at=NOW - 599.0)],
                      FIRST_WINS, now=NOW)
        assert v.action == c.BLOCK


class TestPairsAreGovernedSeparately:
    def test_a_policy_scoped_to_channel_vs_channel_ignores_an_engine(self):
        tg_only = c.Policy("channels only", c.MODE_FIRST_WINS,
                           window_secs=600.0, pairs=(c.PAIR_TELEGRAM_TELEGRAM,))
        v = c.resolve(cand("BUY"), [active("SELL", kind=c.KIND_ENGINE)],
                      tg_only, now=NOW)
        assert v.action == c.ALLOW

    def test_and_fires_on_another_channel(self):
        tg_only = c.Policy("channels only", c.MODE_FIRST_WINS,
                           window_secs=600.0, pairs=(c.PAIR_TELEGRAM_TELEGRAM,))
        v = c.resolve(cand("BUY"),
                      [active("SELL", kind=c.KIND_TELEGRAM, name="Other Channel")],
                      tg_only, now=NOW)
        assert v.action == c.BLOCK

    def test_the_pair_key_does_not_depend_on_argument_order(self):
        assert (c.pair_key(c.KIND_TELEGRAM, c.KIND_ENGINE)
                == c.pair_key(c.KIND_ENGINE, c.KIND_TELEGRAM)
                == c.PAIR_ENGINE_TELEGRAM)


class TestTheModes:
    def test_first_wins_blocks_the_newcomer(self):
        v = c.resolve(cand("BUY"), [active("SELL")], FIRST_WINS, now=NOW)
        assert v.action == c.BLOCK
        assert v.lot_mult == 1.0

    def test_shrink_takes_the_trade_smaller(self):
        v = c.resolve(cand("BUY"), [active("SELL")], SHRINK, now=NOW)
        assert v.action == c.SHRINK
        assert v.lot_mult == 0.5

    def test_freshest_wins_supersedes_an_opposing_signal_that_has_not_filled(self):
        v = c.resolve(cand("BUY"), [active("SELL", is_pending=True, signal_ref="sig-9")],
                      FRESHEST, now=NOW)
        assert v.action == c.SUPERSEDE
        assert v.supersede == ("sig-9",)

    def test_freshest_wins_will_NOT_supersede_one_that_is_already_live(self):
        """Superseding a filled signal means closing a real position. That is
        the frozen close path, and this function may not reach for it."""
        v = c.resolve(cand("BUY"), [active("SELL", is_pending=False)],
                      FRESHEST, now=NOW)
        assert v.action == c.BLOCK
        assert v.supersede == ()
        assert "already live" in v.reason

    def test_a_live_leg_beats_a_pending_one_when_both_oppose(self):
        """Mixed: one cancellable, one not. The uncancellable one decides,
        because allowing the candidate would leave the contradiction standing
        anyway."""
        v = c.resolve(cand("BUY"),
                      [active("SELL", is_pending=True, signal_ref="a"),
                       active("SELL", is_pending=False, name="Breakout Engine")],
                      FRESHEST, now=NOW)
        assert v.action == c.BLOCK

    def test_freshest_wins_ABSTAINS_when_the_fill_state_is_not_recorded(self):
        """None is not False. A policy that refused on a fact it never had
        would report a refusal rate that says nothing about the policy."""
        v = c.resolve(cand("BUY"), [active("SELL", is_pending=None)],
                      FRESHEST, now=NOW)
        assert v.action == c.UNKNOWN
        assert v.supersede == ()

    def test_the_other_modes_do_not_care_about_fill_state(self):
        """Only freshest-wins reads it. If first-wins started abstaining the
        study would lose its most-answerable question to a missing column."""
        unknown = [active("SELL", is_pending=None)]
        assert c.resolve(cand("BUY"), unknown, FIRST_WINS, now=NOW).action == c.BLOCK
        assert c.resolve(cand("BUY"), unknown, SHRINK, now=NOW).action == c.SHRINK

    def test_an_unknown_mode_allows_rather_than_blocking(self):
        """A verdict this function does not understand must not become a
        refusal. Failing open is the behaviour the live path already has."""
        v = c.resolve(cand("BUY"), [active("SELL")],
                      c.Policy("typo", "wnner_takes_all", window_secs=600.0), now=NOW)
        assert v.action == c.ALLOW


class TestTheShippedPolicySet:
    def test_the_champion_is_in_it_and_is_the_off_policy(self):
        """Same rule as decision_shadow: a list of challengers with nothing
        to compare against proves nothing."""
        champions = [p for p in c.POLICIES if p.mode == c.MODE_OFF]
        assert len(champions) == 1
        assert champions[0].is_champion

    def test_every_policy_name_is_unique(self):
        names = [p.name for p in c.POLICIES]
        assert len(names) == len(set(names))

    def test_no_shipped_policy_can_close_a_live_position(self):
        """The one property the whole set has to have. Asserted over the set
        rather than per mode, so a mode added later is covered by it."""
        live = [active("SELL", is_pending=False)]
        for p in c.POLICIES:
            v = c.resolve(cand("BUY"), live, p, now=NOW)
            assert v.supersede == (), f"{p.name} would cancel a filled signal"

    def test_every_shipped_policy_allows_when_nothing_opposes(self):
        for p in c.POLICIES:
            assert c.resolve(cand("BUY"), [active("BUY")], p, now=NOW).action == c.ALLOW
