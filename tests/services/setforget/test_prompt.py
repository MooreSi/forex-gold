"""What the model is actually told.

The prompt had no test of its own until a coverage pass found it, and it is not
a cosmetic surface: it is the entire input to a paid call whose answer can
change the levels on an order ticket. Three things are worth pinning.

**The model is given measurements, never candles.** Handed a series it would
re-derive the structure, disagree with the numbers printed beside it on screen,
and there would be no way to tell which of the two the operator was reading.

**The rules travel with the question.** The system prompt states the method's
six rules and the minimum ratio. A prompt that asks "is this a good trade?"
gets an opinion; one that asks "does this meet these rules?" gets a check.

**Missing evidence renders as missing**, not as a plausible-looking zero. A
confirmation candle that did not happen must not read to the model as one that
did.
"""
from __future__ import annotations

from backend.src.services.setforget import prompt


def _evidence(**over) -> dict:
    ev = {
        "price": 2000.0,
        "weekly_bias": "bullish", "daily_bias": "bullish", "entry_bias": "bullish",
        "entry_timeframe": "4H",
        "zones": [
            {"kind": "demand", "low": 1975.0, "high": 1985.0, "ts": 1.0, "touches": 2},
            {"kind": "supply", "low": 2040.0, "high": 2050.0, "ts": 2.0, "touches": 1},
        ],
        "atr": 6.0, "ema_fast": 1995.0, "ema_slow": 1960.0, "rsi": 52.0,
        "fib": 0.618, "confirmation": None,
    }
    ev.update(over)
    return ev


def _candidate(**over) -> dict:
    row = {"direction": "BUY", "entry": 1985.0, "stop_loss": 1972.0,
           "take_profit": 2040.0, "order_type": "limit", "rr": 4.23}
    row.update(over)
    return row


class TestTheSystemPrompt:
    def test_it_states_the_minimum_ratio_the_code_enforces(self):
        """If these two ever disagree the model is being held to one standard
        and the code to another, and the page shows whichever spoke last."""
        from backend.src.services.setforget import setup

        assert f"1:{setup.MIN_RR:g}" in prompt.SYSTEM

    def test_it_frames_the_model_as_a_reviewer_not_a_scout(self):
        """Asked to FIND a trade a model finds one on every chart, including
        the ones with nothing on them. Asked to judge a specific proposal
        against stated rules, it has something to disagree with."""
        assert "reviewing" in prompt.SYSTEM
        assert "You are not looking for a trade" in prompt.SYSTEM

    def test_it_gives_skip_explicit_permission(self):
        """Without it a model treats "skip" as a failure to be helpful, and
        answers "take" on charts it should have refused."""
        assert 'Use "skip" freely' in prompt.SYSTEM

    def test_it_names_every_key_the_parser_reads(self):
        """A key the parser wants and the prompt never mentions comes back
        missing every time, and the levels are silently never adopted."""
        for key in ("verdict", "entry", "stop_loss", "take_profit",
                    "order_type", "reasoning", "risks"):
            assert f'"{key}"' in prompt.SYSTEM, key

    def test_it_asks_for_bare_json(self):
        assert "no code fence" in prompt.SYSTEM


class TestRender:
    def test_it_carries_the_three_timeframe_reads(self):
        text = prompt.render(_evidence(), _candidate())

        assert "Weekly structure: bullish" in text
        assert "Daily structure:  bullish" in text
        assert "4H" in text

    def test_it_lists_every_zone_with_its_side_of_price(self):
        text = prompt.render(_evidence(), _candidate())

        assert "demand  1975.00 – 1985.00  (below price, tested 2x)" in text
        assert "supply  2040.00 – 2050.00  (above price)" in text

    def test_no_zones_says_none_rather_than_printing_an_empty_heading(self):
        """A bare "Areas of interest:" followed by the next section reads to a
        model as a formatting slip, and it fills the gap itself."""
        text = prompt.render(_evidence(zones=[]), _candidate())

        assert "(none found)" in text

    def test_it_never_ships_a_candle_series(self):
        """The model is given measurements. Handed candles it re-derives the
        structure, disagrees with the numbers printed beside it on screen, and
        nothing says which of the two the operator is reading."""
        ev = _evidence()
        ev["candles"] = [{"ts": 1.0, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}]

        text = prompt.render(ev, _candidate())

        assert "candles" not in text.lower()
        assert '"open"' not in text and "1.5" not in text

    def test_it_states_the_proposal_it_wants_judged(self):
        text = prompt.render(_evidence(), _candidate())

        assert "BUY as a limit order" in text
        assert "Entry:  1985.00" in text
        assert "Stop:   1972.00" in text
        assert "Target: 2040.00" in text
        assert "Reward-to-risk: 1:4.23" in text

    def test_an_unmeasurable_ratio_says_so_rather_than_printing_nothing(self):
        text = prompt.render(_evidence(), _candidate(rr=None))

        assert "unmeasurable" in text

    def test_a_missing_confirmation_candle_is_stated_as_missing(self):
        """The one that would cost money: a bar that did not confirm must not
        read to the model as one that did."""
        text = prompt.render(_evidence(confirmation=None), _candidate())

        assert "no confirmation pattern" in text

    def test_a_confirmation_candle_is_named_in_words(self):
        text = prompt.render(
            _evidence(confirmation={"kind": "pin_bar", "direction": "bullish"}),
            _candidate())

        assert "bullish pin bar" in text

    def test_a_missing_pullback_is_left_out_rather_than_shown_as_zero(self):
        """0.0% is a real reading meaning "price has not pulled back at all".
        Not having a completed leg to measure against is a different state."""
        text = prompt.render(_evidence(fib=None), _candidate())

        assert "Pullback into the last leg" not in text

    def test_an_unavailable_price_does_not_crash_the_render(self):
        """`gather` returns None for price on a disconnected bridge. The
        prompt is only built when there IS a candidate, but a formatter that
        raises would turn a degraded read into a 500."""
        text = prompt.render(_evidence(price=None), _candidate())

        assert "Current price: unavailable" in text

    def test_it_ends_by_asking_for_the_object_only(self):
        text = prompt.render(_evidence(), _candidate())

        assert text.rstrip().endswith("Answer with the JSON object only.")
