"""Saying out loud that an engine has stopped producing.

`docs/simon-handover/034`: the Bounce engine produced its last signal on
2026-08-27 and refused all 514 candidates it found in the sixteen days after.
Nothing said so. Its panel was removed on 2026-09-02, so there is no screen to
put it on — the log is the only place the owner would ever see it, and the log
said nothing either.

`format_silence_warning` is the decision of whether there is anything to say.
Kept apart from the loop that says it so it can be tested without a running
engine, a database or a clock.
"""
from __future__ import annotations

from backend.src.services.test_signal._silence import format_silence_warning

_TWO_DAYS = 48 * 3600


def _report(silent_secs, refusals=(("ml_gate", 369), ("quality_low", 88))):
    return {
        "last_signal_at": 1000.0,
        "silent_secs": silent_secs,
        "candidates": sum(n for _, n in refusals),
        "refusals": list(refusals),
    }


class TestWhenItSpeaks:
    def test_a_long_silence_with_refusals_is_worth_saying(self):
        msg = format_silence_warning(_report(16 * 86400), min_silent_secs=_TWO_DAYS)

        assert msg is not None
        assert "16 days" in msg
        assert "457 candidate" in msg
        assert "ml_gate" in msg

    def test_it_names_the_gate_that_is_actually_binding(self):
        """The commonest refusal is the whole point: "silent" on its own sends
        you looking at the market. "369 of them by the ML gate" sends you to
        the ML gate."""
        msg = format_silence_warning(
            _report(5 * 86400, refusals=(("quality_low", 40), ("ml_gate", 2))),
            min_silent_secs=_TWO_DAYS,
        )

        assert "quality_low" in msg
        assert msg.index("quality_low") < msg.index("ml_gate") if "ml_gate" in msg else True


class TestWhenItStaysQuiet:
    def test_a_short_silence_says_nothing(self):
        """Two days is the floor. Below it, silence is a quiet market and a
        warning every restart would train the reader to ignore this one."""
        assert format_silence_warning(_report(3600), min_silent_secs=_TWO_DAYS) is None

    def test_exactly_the_threshold_does_not_trip_it(self):
        assert format_silence_warning(_report(_TWO_DAYS), min_silent_secs=_TWO_DAYS) is None

    def test_an_engine_that_has_never_produced_a_signal_says_nothing(self):
        """A brand-new install is not a fault, and this would otherwise fire on
        first run, forever, on every machine."""
        report = {"last_signal_at": None, "silent_secs": None,
                  "candidates": 0, "refusals": []}

        assert format_silence_warning(report, min_silent_secs=_TWO_DAYS) is None

    def test_silence_with_nothing_refused_is_the_market_not_a_gate(self):
        """No candidates at all means the engine never found anything to
        judge. That is a quiet fortnight, not a stuck engine, and saying
        "refused 0 candidates" would point at a gate that did nothing."""
        report = _report(16 * 86400, refusals=())

        assert format_silence_warning(report, min_silent_secs=_TWO_DAYS) is None
