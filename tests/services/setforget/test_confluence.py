"""The confluence checklist -- the scored list the G-Club community keeps.

Its whole value is that it says no. A checklist that scores everything highly
is worse than none at all, because it launders a coin flip into a number the
operator will trust. So the tests below are mostly about what does NOT score:
a clean setup taken apart one field at a time, with the score watched down.

The weights are not cosmetic. Higher-timeframe agreement and being at an area
of interest carry two points each because in Alex G's material they are the
entry conditions -- everything else is a reason to like an entry that already
qualifies. A flat checklist would let four weak confirmations outvote the two
that decide whether there is a trade at all.

Missing evidence never scores. A model that cannot see the weekly chart has
not confirmed the weekly chart, and an item that passed because a field was
None is the exact failure mode this file exists to prevent.
"""
from __future__ import annotations

import pytest

from backend.src.services.setforget import confluence


def _clean(**over) -> dict:
    """A textbook long: everything aligned, every field present."""
    ev = {
        "direction": "BUY",
        "weekly_bias": "bullish",
        "daily_bias": "bullish",
        "entry_bias": "bullish",
        "at_zone": {"kind": "demand", "low": 1990.0, "high": 1995.0, "touches": 2},
        "ema_fast": 2005.0,
        "ema_slow": 1980.0,
        "fib": 0.618,
        "rsi": 45.0,
        "confirmation": {"kind": "engulfing", "direction": "bullish"},
    }
    ev.update(over)
    return ev


def _item(result, item_id):
    return next(i for i in result["items"] if i["id"] == item_id)


class TestAClearSetup:
    def test_everything_aligned_scores_full_marks(self):
        result = confluence.score(_clean())

        assert result["score"] == result["max"]
        assert result["grade"] == "high"

    def test_the_mirror_short_scores_the_same(self):
        """Every direction-sensitive item has to be checked the other way
        round. An item that only knows how to read a long silently passes for
        every short, which is half the trades."""
        result = confluence.score(_clean(
            direction="SELL",
            weekly_bias="bearish", daily_bias="bearish", entry_bias="bearish",
            at_zone={"kind": "supply", "low": 2010.0, "high": 2015.0, "touches": 2},
            ema_fast=1980.0, ema_slow=2005.0,
            rsi=55.0,
            confirmation={"kind": "pin_bar", "direction": "bearish"},
        ))

        assert result["score"] == result["max"]

    def test_every_item_says_why_in_words(self):
        """The number is not the product -- the reasons are. A page showing
        7/9 with no account of which two failed tells the operator nothing they
        can act on."""
        for item in confluence.score(_clean())["items"]:
            assert item["detail"], f"{item['id']} scored with no explanation"
            assert item["label"]


class TestHigherTimeframeAgreement:
    def test_a_weekly_that_disagrees_with_the_daily_fails_the_item(self):
        """Alex G's own filter: if the two higher timeframes conflict, the pair
        is too noisy and is skipped. That is this one item."""
        result = confluence.score(_clean(weekly_bias="bearish"))

        assert _item(result, "htf_agreement")["passed"] is False

    def test_both_higher_timeframes_agreeing_against_the_trade_also_fails(self):
        """A perfectly clean bearish weekly and daily is agreement -- and it is
        agreement that this long is wrong. Scoring it would reward the setup
        for the chart being against it."""
        result = confluence.score(_clean(weekly_bias="bearish", daily_bias="bearish"))

        assert _item(result, "htf_agreement")["passed"] is False

    def test_it_is_worth_two_points(self):
        assert _item(confluence.score(_clean()), "htf_agreement")["weight"] == 2


class TestAreaOfInterest:
    def test_no_zone_fails_the_item(self):
        result = confluence.score(_clean(at_zone=None))

        assert _item(result, "at_aoi")["passed"] is False

    def test_a_long_at_a_supply_zone_fails(self):
        """Being at resistance is not a reason to buy. This is the item most
        likely to be waved through by a model that has spotted "a zone"."""
        result = confluence.score(_clean(
            at_zone={"kind": "supply", "low": 2010.0, "high": 2015.0, "touches": 1}))

        assert _item(result, "at_aoi")["passed"] is False

    def test_it_is_worth_two_points(self):
        assert _item(confluence.score(_clean()), "at_aoi")["weight"] == 2


class TestFibonacci:
    @pytest.mark.parametrize("ratio", [0.382, 0.5, 0.618, 0.786])
    def test_a_pullback_inside_the_band_passes(self, ratio):
        assert _item(confluence.score(_clean(fib=ratio)), "fib_zone")["passed"] is True

    @pytest.mark.parametrize("ratio", [0.2, 0.95])
    def test_a_shallow_or_a_broken_pullback_fails(self, ratio):
        """Under 38.2% price has barely pulled back and the zone is not in
        play; over 78.6% the leg is most of the way undone and the structure
        that justified the trade is going with it."""
        assert _item(confluence.score(_clean(fib=ratio)), "fib_zone")["passed"] is False


class TestMomentum:
    def test_an_overbought_rsi_fails_a_long(self):
        assert _item(confluence.score(_clean(rsi=78.0)), "momentum")["passed"] is False

    def test_an_oversold_rsi_fails_a_short(self):
        result = confluence.score(_clean(
            direction="SELL", weekly_bias="bearish", daily_bias="bearish",
            entry_bias="bearish", rsi=22.0))

        assert _item(result, "momentum")["passed"] is False

    def test_an_overbought_rsi_does_not_fail_a_short(self):
        result = confluence.score(_clean(
            direction="SELL", weekly_bias="bearish", daily_bias="bearish",
            entry_bias="bearish", rsi=78.0))

        assert _item(result, "momentum")["passed"] is True


class TestMissingEvidence:
    @pytest.mark.parametrize("field", ["weekly_bias", "daily_bias", "entry_bias",
                                       "at_zone", "ema_fast", "ema_slow",
                                       "fib", "rsi", "confirmation"])
    def test_a_missing_field_never_scores(self, field):
        """The one that would hurt most: an item passing because its input was
        None. The score would rise as the evidence got worse."""
        full = confluence.score(_clean())
        without = confluence.score(_clean(**{field: None}))

        assert without["score"] < full["score"]

    def test_a_missing_field_says_so_rather_than_claiming_a_failure(self):
        result = confluence.score(_clean(rsi=None))

        assert "not available" in _item(result, "momentum")["detail"].lower()


class TestGrade:
    def test_the_grade_follows_the_score(self):
        strong = confluence.score(_clean())
        weak = confluence.score(_clean(
            weekly_bias="bearish", daily_bias="ranging", entry_bias="ranging",
            at_zone=None, ema_fast=1980.0, ema_slow=2005.0, fib=0.1,
            rsi=85.0, confirmation=None))

        assert strong["grade"] == "high"
        assert weak["grade"] == "low"
        assert weak["score"] == 0

    def test_the_percentage_is_of_the_weighted_total_not_the_item_count(self):
        """Four of seven items is not 57% when the three that failed are the
        heavy ones. A count-based percentage would flatter exactly the setups
        the weights exist to catch."""
        result = confluence.score(_clean(at_zone=None, weekly_bias="bearish"))

        assert result["score"] == result["max"] - 4
        assert result["pct"] == pytest.approx(result["score"] / result["max"] * 100)


class TestConfirmationItem:
    def test_a_confirmation_candle_pointing_the_wrong_way_fails(self):
        """A bearish engulfing at a demand zone is not "a confirmation candle
        was present". It is the zone failing, and scoring it would turn the
        clearest signal to stand aside into a reason to enter."""
        result = confluence.score(_clean(
            confirmation={"kind": "engulfing", "direction": "bearish"}))
        item = _item(result, "confirmation")

        assert item["passed"] is False
        assert "bearish" in item["detail"]
        assert "wrong way" in item["detail"]


class TestRetracement:
    def test_no_pullback_at_all_is_zero(self):
        leg = {"start": 1900.0, "end": 2000.0}

        assert confluence.retracement(leg, 2000.0) == pytest.approx(0.0)

    def test_a_fully_undone_leg_is_one(self):
        leg = {"start": 1900.0, "end": 2000.0}

        assert confluence.retracement(leg, 1900.0) == pytest.approx(1.0)

    def test_the_golden_ratio_pullback_reads_as_it_should(self):
        leg = {"start": 1900.0, "end": 2000.0}

        assert confluence.retracement(leg, 1938.2) == pytest.approx(0.618)

    def test_a_downward_leg_is_measured_the_same_way_round(self):
        """A short's impulse runs high to low, so its span is negative. The
        ratio must still come out 0 at the end of the leg and 1 when it is
        undone -- an unsigned version would report every short backwards."""
        leg = {"start": 2000.0, "end": 1900.0}

        assert confluence.retracement(leg, 1900.0) == pytest.approx(0.0)
        assert confluence.retracement(leg, 1961.8) == pytest.approx(0.618)

    def test_no_leg_or_a_flat_one_is_none_rather_than_a_division_by_zero(self):
        assert confluence.retracement(None, 2000.0) is None
        assert confluence.retracement({"start": 2000.0, "end": 2000.0}, 2000.0) is None


class TestRetracementPrices:
    """The inverse of `retracement`: what price a given pullback sits at.

    `retracement` answers "how far has price come back"; this answers "where
    would 61.8% be", which is what the chart draws. They must be exact
    inverses -- a band drawn from one formula and scored by another would show
    price inside the zone while the checklist said it was outside, and nothing
    on screen would say which was right.
    """

    LEG = {"start": 1900.0, "end": 2000.0}

    def test_it_inverts_retracement_exactly(self):
        for ratio in (0.0, 0.382, 0.5, 0.618, 0.786, 1.0):
            price = confluence.retracement_price(self.LEG, ratio)

            assert confluence.retracement(self.LEG, price) == pytest.approx(ratio)

    def test_zero_is_the_end_of_the_leg_and_one_is_its_start(self):
        assert confluence.retracement_price(self.LEG, 0.0) == pytest.approx(2000.0)
        assert confluence.retracement_price(self.LEG, 1.0) == pytest.approx(1900.0)

    def test_a_downward_leg_retraces_upward(self):
        """A short's impulse runs high to low, so its 61.8% sits ABOVE the end
        of the leg. A formula that only knew how to go down would draw every
        short's band on the wrong side of price."""
        down = {"start": 2000.0, "end": 1900.0}

        assert confluence.retracement_price(down, 0.618) == pytest.approx(1961.8)

    def test_no_leg_or_a_flat_one_is_none(self):
        assert confluence.retracement_price(None, 0.5) is None
        assert confluence.retracement_price({"start": 2000.0, "end": 2000.0},
                                            0.5) is None

    def test_the_drawn_levels_bracket_the_band_the_checklist_scores(self):
        """The first and last drawn level ARE the band's edges, so the picture
        and the score cannot disagree about where the zone is."""
        assert confluence.LEVELS[0] == confluence.FIB_LOW
        assert confluence.LEVELS[-1] == confluence.FIB_HIGH

    def test_the_levels_are_the_conventional_four_in_order(self):
        assert confluence.LEVELS == (0.382, 0.5, 0.618, 0.786)
