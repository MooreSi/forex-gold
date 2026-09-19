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

from backend.src.services.setforget import analysis

from ._candles import zigzag


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

        assert [tf for tf, _ in engine.calls] == ["D1", analysis.ENTRY_TIMEFRAME]
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


class TestTheZoneMergeGapIsWired:
    """The fix for the failure that made the whole section useless.

    Over a 400-bar window the detector produces a dozen bands stacked within a
    few points of each other. The next opposing zone is then always a point or
    two from the entry, every candidate comes out under 1:2, and the section
    refuses everything it ever finds -- for a reason that is about the detector
    rather than about the chart.

    `aoi.merge` grew a proximity gap for it. These pin that `gather` actually
    HANDS IT OVER: the merge could be perfect and the orchestrator could still
    call it with the default of zero, which is exactly the shape of bug that
    passes every unit test underneath it.
    """

    @pytest.mark.asyncio
    async def test_the_gap_handed_to_the_zone_builder_is_atr_derived(
            self, monkeypatch):
        seen = []
        monkeypatch.setattr(analysis.aoi, "zones",
                            lambda candles, **kw: seen.append(kw.get("gap")) or [])
        monkeypatch.setattr(analysis.aoi, "merge",
                            lambda raw, **kw: seen.append(kw.get("gap")) or [])
        monkeypatch.setattr(analysis._ict, "atr", lambda candles, period: 8.0)

        engine = _Engine({
            "D1": zigzag([(1900.0, 0), (2000.0, 40), (1960.0, 20)]),
            "H4": zigzag([(1980.0, 0), (2050.0, 30), (2000.0, 30)]),
        })
        await analysis.gather(engine)

        assert seen, "neither zones() nor merge() was called"
        assert all(g == pytest.approx(8.0 * analysis.ZONE_MERGE_ATR) for g in seen), seen

    @pytest.mark.asyncio
    async def test_a_choppy_series_comes_back_with_fewer_zones_than_raw(self):
        """The behaviour, not the wiring. A window that yields a wall of
        hairlines must come back as a handful of levels a person would draw."""
        choppy = zigzag([(2000.0, 0), (2012.0, 3), (2002.0, 3), (2014.0, 3),
                         (2004.0, 3), (2016.0, 3), (2006.0, 3), (2018.0, 3),
                         (2008.0, 3), (2020.0, 3), (2010.0, 3)])
        engine = _Engine({"D1": choppy, "H4": choppy})

        merged = (await analysis.gather(engine))["zones"]
        unmerged = analysis.aoi.zones(choppy, limit=99)

        assert len(merged) < len(unmerged), (len(merged), len(unmerged))

    @pytest.mark.asyncio
    async def test_an_unreadable_atr_does_not_merge_everything_into_one_band(
            self):
        """A gap of zero is the safe failure: separate levels stay separate.
        The dangerous one would be a gap so large that every zone on the chart
        folds into a single band spanning the whole range, which price is
        always "at" -- the checklist would then score every read."""
        engine = _Engine({
            "D1": zigzag([(1900.0, 0), (2000.0, 40), (1960.0, 20), (2060.0, 40)]),
            "H4": [],                                  # no ATR to measure
        })

        ev = await analysis.gather(engine)

        assert ev["atr"] == 0
        assert len(ev["zones"]) > 1


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
