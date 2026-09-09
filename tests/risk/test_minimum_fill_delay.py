"""A signal that fills within moments of being created loses money.

reversal-engine/040. Measured over every executed Reversal Engine signal on
record, re-run 2026-09-09:

| time from signal to fill | n | win % | total |
|---|---|---|---|
| **under 5 min** | **443** | 57.8 | **-$2,142.30** |
| 5-15 min | 115 | **71.3** | **+$1,041.07** |
| 15-30 min | 75 | 57.3 | -$1,009.18 |
| over 30 min | 114 | 58.8 | -$198.73 |

And it is stable, not a fluke of one month:

| | fast (<5m) | 5-15 min |
|---|---|---|
| 2026-07 | -$372.35 | +$14.26 |
| 2026-08 | -$1,376.99 | +$827.87 |
| 2026-09 | -$392.96 | +$198.94 |

The fast bucket loses in every month; the 5-15 bucket wins in every month. The
five largest winners in that bucket are $153/$132/$132/$122/$90 against a
$1,041 total, so no single outlier is carrying it.

**The mechanism is NOT known, and the obvious explanation was tested and
rejected.** The theory was "a fast fill means price was already at or through
the zone when the signal was made, so the level never held". Splitting the same
population on exactly that -- `price_at_signal` already inside or beyond the
entry zone -- barely separates anything:

    already in/through zone       208 trades   57.7%   -$3.55/trade
    price still away from zone    549 trades   59.7%   -$2.91/trade

Both lose, the difference is small, and it captures only 208 of the 443. So
this filter is **empirical**: a consistent effect with no established cause.
That is why it is off by default and why the threshold is configurable rather
than baked in -- a rule whose mechanism is unknown should be easy to turn off
and easy to retune.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import governor


def _rs(on=1, secs=300):
    return {"min_fill_delay_enabled": on, "min_fill_delay_s": secs}


class TestTheToggle:
    def test_off_by_default_nothing_is_refused(self):
        """Absent key behaves as before the setting existed."""
        assert governor.fill_too_soon(created_at=1000.0, now=1001.0, rs={}) is None

    def test_explicitly_off_is_the_same(self):
        assert governor.fill_too_soon(1000.0, 1001.0, _rs(on=0)) is None


class TestWhatItRefuses:
    def test_a_fill_one_second_after_the_signal_is_refused(self):
        reason = governor.fill_too_soon(1000.0, 1001.0, _rs())

        assert reason is not None
        assert "1s" in reason or "1 s" in reason or "1" in reason

    def test_a_fill_just_inside_the_window_is_refused(self):
        assert governor.fill_too_soon(1000.0, 1000.0 + 299, _rs()) is not None

    def test_the_reason_names_the_setting(self):
        """An operator seeing a refusal must be able to find the switch."""
        reason = governor.fill_too_soon(1000.0, 1001.0, _rs())

        assert "trend" not in reason.lower(), "must not be confused with the bias gate"
        assert "fill" in reason.lower() or "soon" in reason.lower()


class TestWhatItMustNotRefuse:
    def test_exactly_at_the_threshold_proceeds(self):
        """The measured buckets are `< 300` and `>= 300`; the boundary belongs
        to the profitable side."""
        assert governor.fill_too_soon(1000.0, 1000.0 + 300, _rs()) is None

    def test_a_later_fill_proceeds(self):
        assert governor.fill_too_soon(1000.0, 1000.0 + 900, _rs()) is None

    @pytest.mark.parametrize("bad", [None, 0, "", -1])
    def test_an_unknown_creation_time_proceeds(self, bad):
        """A signal with no usable creation time cannot be judged. Refusing on
        missing DATA would stop trading whenever a field is absent -- the same
        fail-open rule the bias gate follows."""
        assert governor.fill_too_soon(bad, 5000.0, _rs()) is None

    @pytest.mark.parametrize("bad", [None, 0, "", -1])
    def test_an_unknown_creation_time_proceeds_WHATEVER_THE_CLOCK_SAYS(self, bad):
        """The version above passes even without the missing-value guard,
        because `now=5000` minus a defaulted 0 clears any window on its own --
        proved by mutation, deleting the guard survived it. With a small `now`
        the absent field would be read as "created at the epoch, filled 100s
        ago" and the signal refused for a delay nobody measured.
        """
        assert governor.fill_too_soon(bad, 100.0, _rs()) is None

    def test_a_threshold_of_zero_refuses_nothing(self):
        """Turning the window down to nothing is how an operator disables the
        rule without finding the toggle."""
        assert governor.fill_too_soon(1000.0, 1000.5, _rs(secs=0)) is None

    def test_a_clock_that_goes_backwards_proceeds(self):
        """now < created_at is nonsense, not a fast fill."""
        assert governor.fill_too_soon(5000.0, 1000.0, _rs()) is None


class TestTheThresholdIsConfigurable:
    def test_a_longer_window_refuses_more(self):
        assert governor.fill_too_soon(1000.0, 1000.0 + 600, _rs(secs=900)) is not None

    def test_a_shorter_window_refuses_less(self):
        assert governor.fill_too_soon(1000.0, 1000.0 + 120, _rs(secs=60)) is None

    def test_an_unreadable_threshold_falls_back_to_the_measured_default(self):
        """Garbage in the column must not disable a risk filter silently."""
        rs = {"min_fill_delay_enabled": 1, "min_fill_delay_s": "not a number"}

        assert governor.fill_too_soon(1000.0, 1001.0, rs) is not None
