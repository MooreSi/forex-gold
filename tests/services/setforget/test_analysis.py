"""The orchestrator: evidence in, a candidate trade out, then a model's review.

Three properties carry this file, and all three are about refusing.

**The deterministic candidate is the floor, not the ceiling.** It is built from
the rules before any model is asked anything, so the page is useful -- and
honest -- with no API key, and so there is always something to check the
model's answer against.

**A model's numbers are re-validated, never adopted.** An LLM asked for a stop
and a target will produce a stop and a target every single time, including for
a chart with no setup on it. Everything it returns goes back through
`setup.build` and `setup.invalidations`; what fails is discarded and the
deterministic candidate stands, with the page told why.

**Nothing here places anything.** `evaluate` returns a proposal. The browser
hands it to the existing money endpoints after the operator has read it.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.setforget import analysis, aoi, setup

from ._candles import series, zigzag


class _Engine:
    """Just enough runtime to answer `get_candles`, per timeframe."""

    def __init__(self, by_timeframe: dict, tick: float | None = None):
        self.by_timeframe = by_timeframe
        self.calls: list[tuple[str, int]] = []
        self._tick = tick

    async def get_candles(self, timeframe: str, count: int = 200) -> list[dict]:
        self.calls.append((timeframe, count))
        return self.by_timeframe.get(timeframe, [])


def _zone(kind, low, high, ts=0.0, touches=1):
    return {"kind": kind, "low": low, "high": high, "ts": ts, "touches": touches}


def _evidence(**over) -> dict:
    """A bullish chart with price sitting just above an untouched demand zone."""
    ev = {
        "price": 2000.0,
        "weekly_bias": "bullish",
        "daily_bias": "bullish",
        "entry_bias": "bullish",
        "zones": [_zone("demand", 1975.0, 1985.0), _zone("supply", 2040.0, 2050.0)],
        "atr": 6.0,
        "ema_fast": 1995.0,
        "ema_slow": 1960.0,
        "rsi": 52.0,
        "confirmation": None,
        "impulse": {"start": 1950.0, "end": 2060.0, "start_ts": 1.0, "end_ts": 2.0},
        "fib": 0.545,
        # Gold moves about this much in a day, and it is what decides whether a
        # zone is worth resting an order at -- see TestTheEntryHasToBeReachable.
        "daily_atr": 20.0,
    }
    ev.update(over)
    return ev


class TestGather:
    @pytest.mark.asyncio
    async def test_it_reads_the_daily_and_the_entry_timeframe_from_the_bridge(self):
        engine = _Engine({
            "D1": zigzag([(1900.0, 0), (2000.0, 40), (1960.0, 20), (2060.0, 40)]),
            "H4": zigzag([(1980.0, 0), (2050.0, 30), (2000.0, 30)]),
        })

        ev = await analysis.gather(engine)

        assert [tf for tf, _ in engine.calls] == [
            "D1", analysis.ENTRY_TIMEFRAME, analysis.TRIGGER_TIMEFRAME]
        assert ev["price"] == pytest.approx(
            engine.by_timeframe["H4"][-1]["close"])

    @pytest.mark.asyncio
    async def test_the_weekly_is_built_from_the_dailies_not_asked_for(self):
        """There is no W1 in this app's bridge. Asking for one would return an
        empty list and the weekly bias would silently read "unknown" forever."""
        engine = _Engine({
            "D1": zigzag([(1900.0, 0), (2100.0, 60), (2000.0, 30), (2200.0, 60)]),
            "H4": zigzag([(1980.0, 0), (2050.0, 30), (2000.0, 30)]),
        })

        ev = await analysis.gather(engine)

        assert "W1" not in [tf for tf, _ in engine.calls]
        assert ev["weekly_bias"] in ("bullish", "bearish", "ranging", "unknown")

    @pytest.mark.asyncio
    async def test_no_candles_is_an_unknown_read_rather_than_a_crash(self):
        """A disconnected bridge answers with empty lists. The page has to say
        "no data" -- an exception here would take the whole tab down."""
        ev = await analysis.gather(_Engine({}))

        assert ev["price"] is None
        assert ev["weekly_bias"] == "unknown"
        assert ev["zones"] == []


class TestPropose:
    def test_disagreeing_higher_timeframes_produce_no_candidate(self):
        """Alex G's first filter, and the one that removes the most trades:
        weekly and daily must agree or the pair is skipped."""
        candidate, why = analysis.propose(_evidence(daily_bias="bearish"))

        assert candidate is None
        assert "disagree" in why.lower()

    @pytest.mark.parametrize("bias", ["ranging", "unknown"])
    def test_a_directionless_chart_produces_no_candidate(self, bias):
        candidate, why = analysis.propose(_evidence(weekly_bias=bias, daily_bias=bias))

        assert candidate is None and why

    def test_a_bullish_read_rests_a_buy_limit_at_the_demand_zone_below(self):
        """The set-and-forget entry. Price is at 2000 and the zone is at
        1975-1985, so the order waits at the top of the zone for price to come
        back -- it does not chase."""
        candidate, _ = analysis.propose(_evidence())

        assert candidate is not None
        assert candidate["direction"] == "BUY"
        assert candidate["entry"] == 1985.0
        assert candidate["order_type"] == "limit"

    def test_a_bearish_read_is_the_mirror(self):
        candidate, _ = analysis.propose(_evidence(
            weekly_bias="bearish", daily_bias="bearish", entry_bias="bearish",
            price=2000.0,
            zones=[_zone("supply", 2015.0, 2025.0), _zone("demand", 1930.0, 1940.0)],
        ))

        assert candidate is not None
        assert candidate["direction"] == "SELL"
        assert candidate["entry"] == 2015.0

    def test_price_already_inside_the_zone_is_a_market_order(self):
        candidate, _ = analysis.propose(_evidence(
            price=1980.0, zones=[_zone("demand", 1975.0, 1985.0),
                                 _zone("supply", 2040.0, 2050.0)]))

        assert candidate is not None
        assert candidate["order_type"] == "market"
        assert candidate["entry"] == 1980.0

    def test_the_stop_goes_beyond_the_zone_not_inside_it(self):
        """Alex G's stop is structural: past the level that is meant to hold.
        A stop inside the zone is taken out by the wick that confirms it."""
        candidate, _ = analysis.propose(_evidence())

        assert candidate is not None
        assert candidate["stop_loss"] < 1975.0

    def test_a_confirmation_candle_moves_the_stop_to_its_tail(self):
        """"Below the tail of the pin bar" -- the confirmation candle, when
        there is one, is what the stop is measured from rather than the zone."""
        without, _ = analysis.propose(_evidence(price=1980.0))
        with_pin, _ = analysis.propose(_evidence(
            price=1980.0,
            confirmation={"kind": "pin_bar", "direction": "bullish",
                          "high": 1988.0, "low": 1968.0}))

        assert with_pin is not None and without is not None
        assert with_pin["stop_loss"] < without["stop_loss"]
        assert with_pin["stop_loss"] < 1968.0

    def test_the_target_is_the_next_opposing_zone(self):
        candidate, _ = analysis.propose(_evidence())

        assert candidate is not None
        assert candidate["take_profit"] == 2040.0

    def test_no_opposing_zone_produces_no_candidate(self):
        """With nowhere to take profit there is no reward to measure, so there
        is no way to know whether the trade clears 1:2. Inventing a target from
        a multiple of the risk would be inventing the ratio too."""
        candidate, why = analysis.propose(_evidence(
            zones=[_zone("demand", 1975.0, 1985.0)]))

        assert candidate is None
        assert "take profit" in why.lower()

    def test_no_zone_in_the_direction_of_the_trade_produces_no_candidate(self):
        candidate, why = analysis.propose(_evidence(
            zones=[_zone("supply", 2040.0, 2050.0)]))

        assert candidate is None
        assert why


class TestEvaluate:
    @pytest.fixture
    def engine(self):
        return _Engine({
            "D1": zigzag([(1900.0, 0), (2000.0, 40), (1960.0, 20), (2060.0, 40)]),
            "H4": zigzag([(1980.0, 0), (2050.0, 30), (2000.0, 30)]),
        })

    @pytest.mark.asyncio
    async def test_without_a_configured_model_it_still_returns_the_evidence(
            self, engine, monkeypatch):
        """The checklist, the zones and the deterministic candidate cost
        nothing to compute. A page that could only show them by billing an API
        call would make every glance billable."""
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: False)

        result = await analysis.evaluate(engine, {})

        assert result["ai"] is None
        assert result["billed"] is False
        assert result["confluence"]["max"] > 0

    @pytest.mark.asyncio
    async def test_the_model_is_not_called_when_there_is_no_candidate(
            self, engine, monkeypatch):
        """No setup means nothing to review. Asking anyway costs money to be
        told what the rules already said."""
        called = []
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: True)
        monkeypatch.setattr(analysis._ai, "complete",
                            lambda *a, **k: called.append(a))
        monkeypatch.setattr(analysis, "propose", lambda ev: (None, "no setup"))

        result = await analysis.evaluate(engine, {"ai_provider": "claude"})

        assert called == []
        assert result["billed"] is False
        assert result["candidate"] is None

    @pytest.mark.asyncio
    async def test_a_sound_model_answer_replaces_the_levels(
            self, engine, monkeypatch):
        reply = json.dumps({
            "verdict": "take", "entry": 1984.0, "stop_loss": 1970.0,
            "take_profit": 2040.0, "order_type": "limit",
            "reasoning": "Daily demand, weekly with it.",
            "risks": "NFP on Friday.",
        })
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: True)

        async def _complete(cfg, system, prompt, max_tokens, timeout=30):
            return reply
        monkeypatch.setattr(analysis._ai, "complete", _complete)
        monkeypatch.setattr(analysis, "propose", lambda ev: (
            {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
             "take_profit": 2040.0, "order_type": "limit", "risk": 13.0,
             "reward": 55.0, "rr": 4.23}, ""))

        result = await analysis.evaluate(engine, {"ai_provider": "claude"})

        assert result["billed"] is True
        assert result["candidate"]["stop_loss"] == 1970.0
        assert result["ai"]["verdict"] == "take"
        assert result["ai"]["reasoning"]

    @pytest.mark.asyncio
    async def test_the_review_names_the_model_that_was_actually_called(
            self, engine, monkeypatch):
        """The label beside the verdict has to be the model that produced it.

        `claude_model` carries a config default that is present whether or not
        Claude is the selected provider, so reading the label as
        `claude_model or deepseek_model` printed a Claude model above a
        DeepSeek answer -- a review attributed to a model that was never
        billed.
        """
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: True)

        async def _complete(cfg, system, prompt, max_tokens, timeout=30):
            return json.dumps({"verdict": "skip", "reasoning": "Too far."})
        monkeypatch.setattr(analysis._ai, "complete", _complete)
        monkeypatch.setattr(analysis, "propose", lambda ev: (
            {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
             "take_profit": 2040.0, "order_type": "limit", "risk": 13.0,
             "reward": 55.0, "rr": 4.23}, ""))

        result = await analysis.evaluate(engine, {
            "ai_provider": "deepseek",
            "deepseek_model": "deepseek-flash",
            "claude_model": "claude-sonnet-4-6",
        })

        assert result["ai"]["model"] == "deepseek-flash"

    @pytest.mark.asyncio
    async def test_a_model_answer_that_breaks_the_rules_is_discarded(
            self, engine, monkeypatch):
        """The property this whole file exists for. A model asked for a stop
        will return one for a chart with no setup on it, and a 1:0.6 trade
        renders exactly as convincingly as a 1:3 one."""
        reply = json.dumps({
            "verdict": "take", "entry": 1985.0, "stop_loss": 1970.0,
            "take_profit": 1994.0, "order_type": "limit",     # 1:0.6
            "reasoning": "Looks good to me.",
        })
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: True)

        async def _complete(cfg, system, prompt, max_tokens, timeout=30):
            return reply
        monkeypatch.setattr(analysis._ai, "complete", _complete)
        original = {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
                    "take_profit": 2040.0, "order_type": "limit", "risk": 13.0,
                    "reward": 55.0, "rr": 4.23}
        monkeypatch.setattr(analysis, "propose", lambda ev: (dict(original), ""))

        result = await analysis.evaluate(engine, {"ai_provider": "claude"})

        assert result["candidate"]["stop_loss"] == 1972.0, "the rules' levels stand"
        assert result["candidate"]["take_profit"] == 2040.0
        assert result["ai"]["levels_rejected"]
        assert any("1:2" in r or "1:0.6" in r
                   for r in result["ai"]["levels_rejected"])

    @pytest.mark.asyncio
    async def test_a_model_verdict_of_skip_keeps_the_candidate_and_says_so(
            self, engine, monkeypatch):
        """A skip is information, not an error. The setup stays on screen with
        the model's objection beside it -- hiding it would leave the operator
        unable to judge whether they agree."""
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: True)

        async def _complete(cfg, system, prompt, max_tokens, timeout=30):
            return json.dumps({"verdict": "skip", "reasoning": "News in an hour."})
        monkeypatch.setattr(analysis._ai, "complete", _complete)
        monkeypatch.setattr(analysis, "propose", lambda ev: (
            {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
             "take_profit": 2040.0, "order_type": "limit", "risk": 13.0,
             "reward": 55.0, "rr": 4.23}, ""))

        result = await analysis.evaluate(engine, {"ai_provider": "claude"})

        assert result["ai"]["verdict"] == "skip"
        assert result["candidate"] is not None

    @pytest.mark.asyncio
    async def test_an_unparseable_model_reply_leaves_the_rules_in_charge(
            self, engine, monkeypatch):
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: True)

        async def _complete(cfg, system, prompt, max_tokens, timeout=30):
            return "I'm afraid I can't help with that."
        monkeypatch.setattr(analysis._ai, "complete", _complete)
        monkeypatch.setattr(analysis, "propose", lambda ev: (
            {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
             "take_profit": 2040.0, "order_type": "limit", "risk": 13.0,
             "reward": 55.0, "rr": 4.23}, ""))

        result = await analysis.evaluate(engine, {"ai_provider": "claude"})

        assert result["candidate"]["stop_loss"] == 1972.0
        assert result["ai"]["error"]

    @pytest.mark.asyncio
    async def test_a_provider_that_raises_does_not_take_the_page_down(
            self, engine, monkeypatch):
        monkeypatch.setattr(analysis._ai, "is_configured", lambda cfg: True)

        async def _complete(cfg, system, prompt, max_tokens, timeout=30):
            raise RuntimeError("upstream 529")
        monkeypatch.setattr(analysis._ai, "complete", _complete)
        monkeypatch.setattr(analysis, "propose", lambda ev: (
            {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
             "take_profit": 2040.0, "order_type": "limit", "risk": 13.0,
             "reward": 55.0, "rr": 4.23}, ""))

        result = await analysis.evaluate(engine, {"ai_provider": "claude"})

        assert result["ai"]["error"]
        assert "529" in result["ai"]["error"]


class TestTheAreasOfInterestAreMarkedNotMerged:
    """Replaces `TestTheZoneMergeGapIsWired`, deleted 2026-09-21.

    Those tests pinned the OLD rule: every swing point was a zone, and an
    ATR-derived proximity gap folded the resulting wall of hairlines into
    something usable. The owner replaced that rule -- scan backward on the
    higher timeframes until a band with three touches validates an AOI, then
    stop, and let execution work against those levels alone. So the ATR gap is
    gone, and the tests that asserted it was wired went with it. They were not
    edited to pass; the specification they encoded no longer exists.

    What replaces them is the same KIND of test, which is why the class stays:
    `aoi.mark` could be perfect and `gather` could still call it with the 4H
    series or top it up with unvalidated bands, and every unit test underneath
    would still be green.
    """

    @pytest.mark.asyncio
    async def test_the_levels_come_from_the_higher_timeframes_not_the_4h(self):
        """The 4H is execution. It reacts to the marked levels; it does not
        get to mark its own, or "focuses solely on current price action as it
        interacts with those zones" is not what the page is doing."""
        engine = _Engine({
            # Validated daily levels at 400 and 600, straddling the price.
            "D1": zigzag([(500.0, 0), (600.0, 8), (400.0, 8), (600.0, 8),
                          (400.0, 8), (600.0, 8), (400.0, 8), (520.0, 6)]),
            # The 4H has turned at 520 three times, so it carries a validated
            # band of its own. Price closes at 510, right under it.
            "H4": zigzag([(500.0, 0), (520.0, 8), (500.0, 8), (520.0, 8),
                          (500.0, 8), (520.0, 8), (510.0, 5)]),
        })

        ev = await analysis.gather(engine)

        # Bodies, not wicks, since 2026-09-21 -- so the demand band starts at
        # the swing candle's body low rather than a point below it at the tail.
        assert [z["kind"] for z in ev["zones"]] == ["demand", "supply"], ev["zones"]
        assert round(ev["zones"][0]["low"]) == 400, ev["zones"]
        assert round(ev["zones"][1]["high"]) == 600, ev["zones"]
        assert not any(515.0 <= z["low"] <= 525.0 for z in ev["zones"]), \
            "the 4H marked a level of its own"

    @pytest.mark.asyncio
    async def test_every_marked_zone_carries_three_touches(self):
        engine = _Engine({
            "D1": zigzag([(100.0, 0), (120.0, 8), (100.0, 8), (120.0, 8),
                          (100.0, 8), (120.0, 8), (110.0, 5)]),
            "H4": zigzag([(112.0, 0), (108.0, 10), (110.0, 10)]),
        })

        ev = await analysis.gather(engine)

        assert ev["zones"]
        assert all(z["touches"] >= aoi.MIN_TOUCHES for z in ev["zones"])

    @pytest.mark.asyncio
    async def test_the_page_is_told_how_far_back_the_levels_were_marked(self):
        """The question that started this ("does it really measure back to the
        14th of September?") had no answer on screen. Now it does."""
        daily = zigzag([(100.0, 0), (120.0, 8), (100.0, 8), (120.0, 8),
                        (100.0, 8), (120.0, 8), (110.0, 5)])
        engine = _Engine({
            "D1": daily,
            "H4": zigzag([(112.0, 0), (108.0, 10), (110.0, 10)]),
        })

        ev = await analysis.gather(engine)

        assert ev["zone_scan"]["daily_bars"] > 0
        assert ev["zone_scan"]["daily_bars"] <= len(daily)

    @pytest.mark.asyncio
    async def test_a_chart_with_no_validated_level_marks_none(self):
        """A one-way trend has turned at nothing three times. The honest answer
        is no levels -- and `propose` then refuses for a reason that names the
        chart rather than the detector. Topping the list up with unvalidated
        bands is exactly what the three-touch rule exists to refuse."""
        engine = _Engine({
            "D1": zigzag([(1000.0, 0), (2000.0, 80), (1990.0, 6)]),
            "H4": zigzag([(1995.0, 0), (1990.0, 10), (1992.0, 10)]),
        })

        ev = await analysis.gather(engine)

        assert ev["zones"] == []


class TestProposeWithoutAPrice:
    def test_a_bridge_that_returned_nothing_is_named_rather_than_crashing(self):
        """A disconnected bridge answers with empty lists, so price is None.
        Every comparison below it would raise on None, and the tab would show
        a 500 instead of "check the MT5 connection"."""
        candidate, why = analysis.propose(_evidence(price=None))

        assert candidate is None
        assert "MT5" in why


class TestParsingTheModelsReply:
    """How the JSON arrives, which is never quite how it was asked for.

    The prompt says "no code fence". Models fence anyway, and they preface the
    object with a sentence. A parser that only accepts the clean case rejects
    most real replies -- and the failure is invisible: the page falls back to
    the rules' levels, shows a polite "the reply was not JSON", and everyone
    concludes the model is unhelpful rather than that the parser is strict.
    """

    def test_a_bare_object(self):
        assert analysis._parse('{"verdict": "take"}') == {"verdict": "take"}

    @pytest.mark.parametrize("fence", ["```", "```json", "```JSON"])
    def test_a_fenced_object(self, fence):
        raw = f'{fence}\n{{"verdict": "skip", "reasoning": "News."}}\n```'

        assert analysis._parse(raw)["verdict"] == "skip"

    def test_an_object_with_a_sentence_in_front_of_it(self):
        """A model that wrote a preamble still answered. The object is what
        matters; the apology around it is not."""
        raw = 'Here is my review:\n\n{"verdict": "adjust", "entry": 1984.0}'

        parsed = analysis._parse(raw)

        assert parsed["verdict"] == "adjust"
        assert parsed["entry"] == 1984.0

    def test_an_object_with_prose_on_both_sides(self):
        raw = 'Sure.\n{"verdict": "take"}\nLet me know if you want more.'

        assert analysis._parse(raw)["verdict"] == "take"

    def test_leading_and_trailing_whitespace(self):
        assert analysis._parse('\n\n  {"verdict": "take"}  \n') == {"verdict": "take"}

    def test_a_reply_with_no_object_in_it_raises_rather_than_guessing(self):
        """`evaluate` catches this and says the levels were not used. Returning
        an empty dict instead would look like a model that answered with no
        opinion, which is a different thing entirely."""
        with pytest.raises(Exception):
            analysis._parse("I'm afraid I can't help with that.")


class TestTheStopForAShort:
    """The BUY path is covered above. Every direction-sensitive branch needs
    the mirror: a stop computed the wrong way round for a short sits INSIDE
    the zone, gets taken out by the wick that confirms the setup, and every
    number printed beside it still agrees with itself."""

    def _short(self, **over):
        ev = _evidence(
            weekly_bias="bearish", daily_bias="bearish", entry_bias="bearish",
            price=2000.0,
            zones=[_zone("supply", 2015.0, 2025.0), _zone("demand", 1930.0, 1940.0)],
        )
        ev.update(over)
        return ev

    def test_the_stop_goes_above_the_zone(self):
        candidate, _ = analysis.propose(self._short())

        assert candidate is not None
        assert candidate["stop_loss"] > 2025.0

    def test_a_bearish_confirmation_candle_pushes_the_stop_above_its_wick(self):
        without, _ = analysis.propose(self._short(price=2020.0))
        with_pin, _ = analysis.propose(self._short(
            price=2020.0,
            confirmation={"kind": "pin_bar", "direction": "bearish",
                          "high": 2032.0, "low": 2018.0}))

        assert with_pin is not None and without is not None
        assert with_pin["stop_loss"] > without["stop_loss"]
        assert with_pin["stop_loss"] > 2032.0

    def test_a_confirmation_candle_pointing_the_wrong_way_is_ignored(self):
        """A BULLISH pin bar at a supply zone is the zone failing. Measuring a
        short's stop from it would put the stop below the entry."""
        ignored, _ = analysis.propose(self._short(
            price=2020.0,
            confirmation={"kind": "pin_bar", "direction": "bullish",
                          "high": 2090.0, "low": 2018.0}))
        plain, _ = analysis.propose(self._short(price=2020.0))

        assert ignored is not None and plain is not None
        assert ignored["stop_loss"] == plain["stop_loss"]


class TestTheZoneTolerance:
    """How close to a zone counts as being at it. ATR, because what counts as
    close is a property of how far the instrument moves in a bar."""

    def test_it_is_the_atr_when_there_is_one(self):
        assert analysis.tolerance({"atr": 6.0, "price": 2000.0}) == 6.0

    def test_an_unreadable_atr_falls_back_to_a_fraction_of_price(self):
        """Zero tolerance would mean the setup exists only on the bar that
        happens to print inside the band -- so a flat or empty series would
        read as "price is never at a zone" rather than as missing data."""
        fallback = analysis.tolerance({"atr": 0.0, "price": 2000.0})

        assert 0 < fallback < 6.0
        assert fallback == pytest.approx(2000.0 * analysis._FALLBACK_TOLERANCE_PCT)

    def test_no_price_and_no_atr_is_zero_rather_than_an_error(self):
        assert analysis.tolerance({}) == 0.0


class TestTheModelsPartialReplies:
    """A reply that parsed but did not carry all three levels.

    Common: a model that says "take" and leaves the levels off, meaning "as
    proposed". Its verdict is still worth showing; its silence must not be read
    as a level of zero.
    """

    def _review(self, reply):
        candidate = {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
                     "take_profit": 2040.0, "order_type": "limit", "rr": 4.23}
        return analysis._review_levels(reply, candidate, 2000.0, 6.0)

    def test_a_verdict_with_no_levels_leaves_the_candidate_alone(self):
        revised, rejected = self._review({"verdict": "take"})

        assert revised is None
        assert rejected == []

    def test_two_of_three_levels_is_not_enough_to_replace_anything(self):
        """Taking the two it gave and keeping the third would build a setup
        nobody proposed -- half the model's and half the rules'."""
        revised, rejected = self._review(
            {"verdict": "adjust", "entry": 1984.0, "stop_loss": 1970.0})

        assert revised is None
        assert rejected == []

    def test_a_level_sent_as_a_string_is_not_silently_coerced(self):
        """"1984.0" is a number to `float()` and not to the type check. A model
        that quotes its numbers is a model whose reply was not the shape asked
        for, and guessing what it meant is how a typo becomes an order."""
        revised, _ = self._review({"verdict": "take", "entry": "1984.0",
                                   "stop_loss": 1970.0, "take_profit": 2040.0})

        assert revised is None

    def test_a_skip_never_replaces_the_levels_even_when_it_sends_some(self):
        revised, rejected = self._review(
            {"verdict": "skip", "entry": 1984.0, "stop_loss": 1970.0,
             "take_profit": 2040.0})

        assert revised is None
        assert rejected == []


class TestTheDrawableFibonacciLevels:
    """The chart draws the retracement band, so `gather` has to carry it.

    Computed here rather than in the browser for the same reason as everything
    else: `confluence.retracement_price` is the inverse of the function the
    checklist scores with, and a TypeScript copy would be a second answer to
    where 61.8% is -- visible as a band that disagrees with the tick beside it.
    """

    @pytest.mark.asyncio
    async def test_every_level_comes_back_with_its_ratio_and_its_price(self):
        engine = _Engine({
            "D1": zigzag([(1900.0, 0), (2000.0, 40), (1960.0, 20), (2060.0, 40)]),
            # Enough legs for a COMPLETED impulse: a swing and the swing before
            # it. Two turns is the minimum, and a series that only rises has no
            # leg at all -- which is what the third test below is about.
            "H4": zigzag([(1980.0, 0), (2060.0, 10), (2010.0, 10),
                          (2090.0, 10), (2040.0, 10)]),
        })

        levels = (await analysis.gather(engine))["fib_levels"]

        assert [l["ratio"] for l in levels] == list(analysis.confluence.LEVELS)
        assert all(isinstance(l["price"], float) for l in levels)

    @pytest.mark.asyncio
    async def test_the_levels_agree_with_the_pullback_the_checklist_scores(self):
        """The property worth more than the values: draw and score from the
        same leg. A band drawn over one impulse and a ratio measured over
        another is two right answers to different questions."""
        engine = _Engine({
            "D1": zigzag([(1900.0, 0), (2000.0, 40), (1960.0, 20), (2060.0, 40)]),
            "H4": zigzag([(1980.0, 0), (2060.0, 10), (2010.0, 10),
                          (2090.0, 10), (2040.0, 10)]),
        })

        ev = await analysis.gather(engine)
        band = [l["price"] for l in ev["fib_levels"]]
        assert band, "the fixture must have a completed leg to measure"
        inside = min(band) <= ev["price"] <= max(band)

        assert inside == (analysis.confluence.FIB_LOW
                          <= (ev["fib"] or -1) <= analysis.confluence.FIB_HIGH)

    @pytest.mark.asyncio
    async def test_no_completed_leg_means_no_levels_rather_than_levels_of_none(
            self):
        """A list of nulls would be drawn as a band at zero, across the bottom
        of the chart, looking like a real level nobody can explain."""
        engine = _Engine({"D1": [], "H4": []})

        assert (await analysis.gather(engine))["fib_levels"] == []


class TestTheEntryHasToBeReachable:
    """Reported 2026-09-21: a resting order was placed at a zone price would
    take days to reach, on a method whose whole premise is a week of planning.

    The cause was that `propose` took the NEAREST qualifying zone with no
    ceiling on how far away that was. On a window of several months of daily
    and weekly levels, the nearest one below price can be hundreds of dollars
    off -- a perfectly real level, and not one this week's order belongs at.

    Distance is measured in DAILY ATR because "how far away is it" only means
    anything as "how long would it take": 400 points is a fortnight on a quiet
    gold and two sessions on a violent one.
    """

    def _far(self, **over):
        # The demand zone sits 300 below price: fifteen average days away.
        ev = _evidence(
            price=2000.0, daily_atr=20.0,
            zones=[_zone("demand", 1690.0, 1700.0), _zone("supply", 2040.0, 2050.0)],
        )
        ev.update(over)
        return ev

    def test_a_zone_days_away_is_refused_rather_than_rested_at(self):
        candidate, why = analysis.propose(self._far())

        assert candidate is None
        assert why

    def test_the_refusal_says_how_far_and_how_long(self):
        """"No setup" is useless here. The operator needs to know whether to
        come back tomorrow or next month, and the distance alone does not say
        -- 300 points is a fortnight on a quiet gold and two sessions on a
        violent one."""
        _, why = analysis.propose(self._far())

        assert "300" in why
        assert "15" in why and "day" in why.lower()

    def test_a_zone_within_reach_is_still_rested_at(self):
        """The negative control: the ceiling must not refuse everything."""
        near = self._far(zones=[_zone("demand", 1950.0, 1960.0),
                                _zone("supply", 2040.0, 2050.0)])

        candidate, why = analysis.propose(near)

        assert candidate is not None, why
        assert candidate["entry"] == 1960.0

    def test_the_ceiling_scales_with_how_far_gold_actually_moves(self):
        """Same 40-point gap, two different markets. On a quiet gold that is
        four days out and worth waiting for; on a violent one it is half a
        session. A fixed point ceiling would be wrong on both."""
        zones = [_zone("demand", 1950.0, 1960.0), _zone("supply", 2040.0, 2050.0)]
        quiet, _ = analysis.propose(_evidence(price=2000.0, zones=zones,
                                              daily_atr=2.0))
        busy, _ = analysis.propose(_evidence(price=2000.0, zones=zones,
                                             daily_atr=40.0))

        assert quiet is None, "40 points is 20 days at an ATR of 2"
        assert busy is not None

    def test_the_candidate_carries_the_distance_so_the_page_can_say_it(self):
        candidate, _ = analysis.propose(_evidence())

        assert candidate is not None
        assert candidate["distance"] == pytest.approx(15.0)      # 2000 - 1985
        assert candidate["distance_days"] == pytest.approx(0.75)  # at ATR 20

    def test_a_market_entry_is_zero_days_away_not_unmeasured(self):
        """Price is already at the zone. Zero is the honest answer and None
        would render as an em dash next to a trade that fills now."""
        candidate, _ = analysis.propose(_evidence(price=1980.0))

        assert candidate is not None
        assert candidate["order_type"] == "market"
        assert candidate["distance_days"] == pytest.approx(0.0)

    def test_an_unreadable_daily_atr_does_not_refuse_everything(self):
        """A bridge that could not serve daily candles must not turn the whole
        section into "nothing is reachable" -- that reads as a market with no
        setups rather than as missing data. With no ATR the ceiling cannot be
        computed, so it is not applied, and the distance is reported unscaled."""
        candidate, why = analysis.propose(self._far(daily_atr=0.0))

        assert candidate is not None, why
        assert candidate["distance_days"] is None


class TestAWideBandCannotCarryAnEntry:
    """`aoi.within_width` is the rail; this is the wiring that arms it.

    The rail could be perfect and `propose` could never call it -- the same
    shape of bug as a merge gap that is computed and then not handed over. So
    this checks the decision, not the helper.

    The band is still REPORTED by `gather` and still drawn on the chart. It is
    real structure; it is just not a precise enough level to place an order at.
    Those are different claims and the code makes them separately.
    """

    def test_a_band_wide_enough_to_swallow_price_cannot_be_the_entry(self):
        """Measured live on 2026-09-21: a 1035-point band on gold at ~$4,350.
        Price is inside one that wide essentially always, so it made "price is
        at an area of interest" trivially true."""
        candidate, why = analysis.propose(_evidence(
            price=4350.0, daily_atr=30.0,
            zones=[_zone("demand", 3315.0, 4349.0, touches=80),
                   _zone("supply", 4400.0, 4410.0, touches=3)]))

        assert candidate is None
        assert why

    def test_a_band_wide_enough_to_swallow_price_cannot_be_the_target(self):
        """A take-profit inside a thousand-point band is a target with a
        thousand points of slack, and the ratio computed from it is fiction."""
        candidate, why = analysis.propose(_evidence(
            price=4350.0, daily_atr=30.0,
            zones=[_zone("demand", 4330.0, 4345.0, touches=3),
                   _zone("supply", 4400.0, 5435.0, touches=80)]))

        assert candidate is None
        assert "take profit" in why.lower()

    def test_ordinary_bands_are_left_alone(self):
        """The negative control. A cap that refuses everything reads as a
        market with no structure in it, which is worse than no cap at all."""
        candidate, why = analysis.propose(_evidence(
            price=4350.0, daily_atr=30.0,
            zones=[_zone("demand", 4330.0, 4345.0, touches=3),
                   _zone("supply", 4400.0, 4415.0, touches=3)]))

        assert candidate is not None, why
        # Price is within ATR of the band, so this is a market entry off it.
        assert candidate["zone"]["low"] == 4330.0
        assert candidate["take_profit"] == 4400.0


class TestTheThreeStages:
    """The entry model, rebuilt on 2026-09-21 after the owner confirmed the
    trigger belongs on the 30-minute chart.

    The old model had two stages: pick a zone, rest an order at it. That is
    what put an order days of travel from price -- there was nothing to wait
    for, so the app committed immediately and let the market come to it.

    The new model has three, and only the third may be placed:

      armed      -- the zone is chosen, price has not reached it
      waiting    -- price is at the zone, the 30m has not reacted yet
      triggered  -- price is at the zone AND the 30m has shifted or engulfed

    The first two are still SHOWN, with their levels, so the plan is visible
    before it is live. They are just not placeable, and `setup.invalidations`
    is what refuses them -- the same mechanism that refuses a thin ratio, so
    the Execute button is disabled with a reason rather than mysteriously.
    """

    # Falls, bounces to a swing high, falls again, then closes back through it.
    TURNED_UP = [118, 112, 106, 120, 108, 102, 98, 104, 112, 126]

    def _ev(self, **over):
        ev = _evidence(
            price=1980.0,
            zones=[_zone("demand", 1975.0, 1985.0, touches=3),
                   _zone("supply", 2040.0, 2050.0, touches=3)],
        )
        ev.update(over)
        return ev

    def test_price_not_yet_at_the_zone_is_armed_and_not_placeable(self):
        """The state the old model placed an order in."""
        candidate, why = analysis.propose(self._ev(price=2000.0))

        assert candidate is not None, why
        assert candidate["stage"] == "armed"
        assert setup.invalidations(candidate), "armed must not be placeable"

    def test_at_the_zone_with_a_quiet_30m_is_waiting(self):
        candidate, why = analysis.propose(self._ev(
            trigger_candles=series([118, 112, 106, 120, 108, 102, 98, 96, 94])))

        assert candidate is not None, why
        assert candidate["stage"] == "waiting"
        assert setup.invalidations(candidate), "waiting must not be placeable"

    def test_at_the_zone_with_a_shift_is_triggered_and_placeable(self):
        candidate, why = analysis.propose(self._ev(
            trigger_candles=series(self.TURNED_UP)))

        assert candidate is not None, why
        assert candidate["stage"] == "triggered"
        assert setup.invalidations(candidate) == []

    def test_a_triggered_setup_enters_at_the_market(self):
        """The guide: "enter immediately after a signal candle closes". The
        whole point of waiting for the 30m is that price is already at the
        level when it fires -- so there is nothing left to rest an order for."""
        candidate, _ = analysis.propose(self._ev(
            trigger_candles=series(self.TURNED_UP)))

        assert candidate is not None
        assert candidate["order_type"] == "market"

    def test_the_refusal_names_the_stage_rather_than_saying_no(self):
        """"Not placeable" is useless. "Waiting for a 30m shift of structure"
        tells the operator what they are waiting FOR, which is the difference
        between a plan and a broken page."""
        armed, _ = analysis.propose(self._ev(price=2000.0))
        waiting, _ = analysis.propose(self._ev(
            trigger_candles=series([118, 112, 106, 120, 108, 102, 98, 96, 94])))

        assert any("reach" in r.lower() or "arriv" in r.lower()
                   for r in setup.invalidations(armed)), setup.invalidations(armed)
        assert any("30" in r for r in setup.invalidations(waiting)), \
            setup.invalidations(waiting)

    def test_the_trigger_travels_with_the_candidate(self):
        """So the card can name what fired, and the operator can disagree."""
        candidate, _ = analysis.propose(self._ev(
            trigger_candles=series(self.TURNED_UP)))

        assert candidate is not None
        assert candidate["trigger"]["kind"] == "shift_of_structure"

    def test_no_30m_data_at_all_leaves_it_waiting_rather_than_triggered(self):
        """A bridge that served no 30m candles must not read as "nothing has
        happened, therefore go". Missing data is not a quiet market."""
        candidate, _ = analysis.propose(self._ev(trigger_candles=[]))

        assert candidate is not None
        assert candidate["stage"] == "waiting"
        assert setup.invalidations(candidate)


class TestGatherReadsTheTriggerTimeframe:
    @pytest.mark.asyncio
    async def test_it_asks_the_bridge_for_the_30m(self):
        engine = _Engine({
            "D1": zigzag([(1900.0, 0), (2000.0, 40), (1960.0, 20), (2060.0, 40)]),
            "H4": zigzag([(1980.0, 0), (2050.0, 30), (2000.0, 30)]),
            "M30": zigzag([(1990.0, 0), (2010.0, 20), (2000.0, 20)]),
        })

        await analysis.gather(engine)

        assert analysis.TRIGGER_TIMEFRAME in [tf for tf, _ in engine.calls]

    @pytest.mark.asyncio
    async def test_the_30m_candles_do_not_ship_to_the_browser(self):
        """Same rule as the other three series: the page draws from
        /api/chart at its own window, and a second copy here is a second
        answer to what the last bar was."""
        engine = _Engine({"D1": [], "H4": [], "M30": []})

        ev = await analysis.gather(engine)

        assert "trigger_candles" not in analysis.public_evidence(ev)
