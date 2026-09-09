"""The shipped Logic Keyword defaults are the owner's live set.

He asked (2026-09-09) that the keywords he had added in the app be captured in
the repo, so a reinstall or another client starts with them. His live lexicons
were read from `logic_keyword_lexicons` and folded into `DEFAULT_LEXICONS`.

What changed against the previous defaults:

* `buy_orders`  +BUY, +BUY GOLD @, +SCALP BUY NOW, +PREPARE FOR A BUY, -BUY ZONE
* `sell_orders` +PREPARE FOR A SELL, +SCALP SELL NOW, +SELL GOLD @
* `limit_orders` +BUY ZONE, +SELL ZONE, +NEXT BUY ZONE, +NEXT SELL ZONE,
                 +FUTURE BUY LIMIT, +FUTURE SELL LIMIT,
                 -BUY GOLD @, -SELL GOLD @

**The two lexicons are not the same KIND of list, and that governs everything
here.** `keywords.py`'s own docstring: `buy_orders`/`sell_orders` are **live
direction triggers**; `limit_orders` is a *"reference list ... matched only by
the AI-fallback gate"*. Moving a phrase into `limit_orders` does not create a
limit order — the pending-order layout is matched by a regex in `parser.py`
that no lexicon feeds.

So a phrase appearing in both a market lexicon and `limit_orders` is NOT a
conflict; they are consulted by different things for different purposes. An
earlier version of this file asserted otherwise and was wrong.

`parse_lexicon_direction_trigger` refuses on two counts that matter here: the
match is **per-line and exact**, and **any message stating a number is refused
outright**. Both are verified below against the real function, because they are
what make a bare "BUY" narrow rather than reckless — and what makes one of his
entries inert.
"""
from __future__ import annotations

import pytest

from backend.src.services.telegram.keywords import DEFAULT_LEXICONS


class TestNoLexiconRepeatsItself:
    """His live `limit_orders` contained "BUY ZONE" twice. Harmless to match
    against, but it is a list a human edits, and a duplicate is the kind of
    thing that hides a typo sitting next to it."""

    @pytest.mark.parametrize("category", sorted(DEFAULT_LEXICONS))
    def test_no_duplicates(self, category):
        phrases = DEFAULT_LEXICONS[category]

        assert len(phrases) == len(set(phrases)), (
            f"{category} repeats: "
            f"{sorted({p for p in phrases if phrases.count(p) > 1})}"
        )

    @pytest.mark.parametrize("category", sorted(DEFAULT_LEXICONS))
    def test_no_blank_or_untrimmed_entries(self, category):
        for phrase in DEFAULT_LEXICONS[category]:
            assert phrase == phrase.strip() != "", f"{category}: {phrase!r}"


class TestTheOwnersAdditionsAreShipped:
    @pytest.mark.parametrize("phrase", [
        "BUY GOLD @", "SCALP BUY NOW", "PREPARE FOR A BUY",
    ])
    def test_his_buy_phrases(self, phrase):
        assert phrase in DEFAULT_LEXICONS["buy_orders"]

    @pytest.mark.parametrize("phrase", [
        "PREPARE FOR A SELL", "SCALP SELL NOW", "SELL GOLD @",
    ])
    def test_his_sell_phrases(self, phrase):
        assert phrase in DEFAULT_LEXICONS["sell_orders"]

    @pytest.mark.parametrize("phrase", [
        "BUY ZONE", "SELL ZONE", "NEXT BUY ZONE", "NEXT SELL ZONE",
        "FUTURE BUY LIMIT", "FUTURE SELL LIMIT",
    ])
    def test_his_zone_references(self, phrase):
        assert phrase in DEFAULT_LEXICONS["limit_orders"]

    def test_he_removed_BUY_ZONE_as_a_market_trigger(self):
        """Deliberate on his part: "BUY ZONE" no longer opens at market."""
        assert "BUY ZONE" not in DEFAULT_LEXICONS["buy_orders"]


class TestWhatTheseTriggersActuallyDo:
    """Asserted against the real matcher, not against the list.

    These are what make a bare "BUY" a narrow trigger rather than a reckless
    one, and they are easy to lose in a refactor of the matcher.
    """

    @pytest.fixture(autouse=True)
    def _defaults_not_the_database(self, monkeypatch):
        """`get_lexicon` reads the live table. Point it at the SHIPPED
        defaults instead: these tests are about what this repo ships, and a
        test that needed the owner's own database could not run anywhere
        else."""
        from backend.src.services.telegram import keyword_triggers as kt

        monkeypatch.setattr(
            kt, "get_lexicon", lambda cat: list(DEFAULT_LEXICONS.get(cat, [])))

    @staticmethod
    def _trig(text):
        from backend.src.services.telegram.keyword_triggers import (
            parse_lexicon_direction_trigger,
        )
        return parse_lexicon_direction_trigger(text)

    def test_a_shipped_phrase_fires(self):
        assert self._trig("PREPARE FOR A BUY") == ("BUY", None)

    def test_a_bare_word_fires_ONLY_if_someone_adds_one(self, monkeypatch):
        """The owner has one in his install; the defaults do not ship it. This
        shows what it does when added, without shipping it -- the mechanism is
        real, which is exactly why the 2026-08-27 directive keeps it out of the
        defaults."""
        from backend.src.services.telegram import keyword_triggers as kt
        monkeypatch.setattr(kt, "get_lexicon",
                            lambda cat: ["BUY"] if cat == "buy_orders" else [])

        assert self._trig("BUY") == ("BUY", None)
        # and even then, only as a whole line with no numbers anywhere
        assert self._trig("Some chat mentioning BUY in a sentence") is None
        assert self._trig("BUY 4394") is None

    def test_a_shipped_phrase_inside_a_sentence_does_not_fire(self):
        """Per-line and exact. As a substring this would fire on most of what
        a gold channel says in a day."""
        assert self._trig("we might PREPARE FOR A BUY later today") is None

    def test_and_any_message_stating_a_number_is_refused(self):
        """A message with levels is a signal, and belongs to the per-format
        parsers rather than to a market order that would ignore them."""
        assert self._trig("BUY GOLD @ 4394/4388") is None

    def test_which_makes_the_owners_BUY_GOLD_AT_entry_INERT(self):
        """Worth knowing rather than assuming. "BUY GOLD @" only fires on a
        line that is exactly that and states no price -- and a real message
        using it always carries one, as the case above shows. The entry is
        harmless, but it will not do what its wording suggests.
        """
        assert self._trig("BUY GOLD @") == ("BUY", None)      # the bare phrase
        assert self._trig("BUY GOLD @ 4394/4388") is None      # any real message


class TestTheOwnersBareBUYIsTheOneThingNOTShipped:
    """He has a bare "BUY" in his own install. It is not in the defaults.

    An earlier version of this file shipped it, and
    `test_lexicon_direction_triggers.test_no_shipped_default_is_a_single_bare_word`
    went red -- correctly. That test carries an owner directive from
    2026-08-27, made after bare BUY/SELL shipped briefly and were pulled: a
    lone "BUY" posted as commentary is indistinguishable from one posted as an
    instruction, and line-exact matching does not help with that. It says in
    terms that a bare word is "still addable in the UI; it just cannot arrive
    by default".

    So his install keeps it -- it lives in his `logic_keyword_lexicons` row and
    nothing here touches that -- and a fresh install does not inherit it.
    """

    def test_no_bare_buy_in_the_shipped_defaults(self):
        assert "BUY" not in DEFAULT_LEXICONS["buy_orders"]

    def test_nor_a_bare_sell(self):
        assert "SELL" not in DEFAULT_LEXICONS["sell_orders"]
