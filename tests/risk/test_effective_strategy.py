"""Which strategy a trade actually opens with, by time of day.

`get_effective_strategy` decides whether the Out of Hours strategy replaces the
configured one. That is not cosmetic: strategies differ in their partial-close
ladder, their breakeven trigger and their trailing, so getting it wrong means
the trade is managed by rules the operator did not choose for that hour.

Half of it was uncovered. It reads the clock internally, so it took an optional
`now` to become testable — the same shape `check_trading_schedule(now=...)`
already uses in this codebase, and the default is unchanged.

Note the clock: OOH is evaluated in **UTC**, unlike the Trading Schedule, which
moved to UK time on 2026-09-01. That is defensible — "out of hours" is about
the market's quiet stretch rather than the operator's day — but it is the same
kind of ambiguity the schedule had, so it is asserted here rather than left
implicit, and raised in docs/simon-handover/020.
"""
from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from backend.src.services.risk.risk_settings_repo import get_effective_strategy


def _rs(**over):
    rs = {"trade_strategy": "scale_out", "ooh_enabled": 1,
          "ooh_strategy": "conservative",
          "ooh_start_time": "22:00", "ooh_end_time": "07:00",
          "ooh_date_active": 0, "ooh_date_from": "", "ooh_date_to": ""}
    rs.update(over)
    return rs


def _at(hh, mm=0, d=(2026, 6, 15)):
    return datetime(d[0], d[1], d[2], hh, mm, tzinfo=timezone.utc)


class TestWhenOutOfHoursIsOff:
    def test_the_configured_strategy_is_used(self):
        assert get_effective_strategy(_rs(ooh_enabled=0), now=_at(23)) == \
            ("scale_out", False)

    def test_even_inside_what_would_be_the_window(self):
        assert get_effective_strategy(_rs(ooh_enabled=0), now=_at(2))[1] is False

    def test_a_missing_strategy_falls_back_rather_than_returning_empty(self):
        """An empty strategy name would reach open_trade and match nothing."""
        assert get_effective_strategy(
            {"trade_strategy": "", "ooh_enabled": 0}, now=_at(12))[0] == "scale_out"


class TestAWindowThatSpansMidnight:
    """22:00-07:00 — the default, and the case a naive start<=now<end gets
    wrong for nine hours a day."""

    @pytest.mark.parametrize("hh", [22, 23, 0, 3, 6])
    def test_inside_the_window_uses_the_ooh_strategy(self, hh):
        assert get_effective_strategy(_rs(), now=_at(hh)) == \
            ("conservative", True)

    @pytest.mark.parametrize("hh", [7, 8, 12, 21])
    def test_outside_it_uses_the_base_strategy(self, hh):
        assert get_effective_strategy(_rs(), now=_at(hh)) == ("scale_out", False)

    def test_the_boundaries_are_half_open(self):
        """22:00 is in, 07:00 is out — an off-by-one here silently moves an
        hour of the day onto the wrong ladder."""
        assert get_effective_strategy(_rs(), now=_at(22, 0))[1] is True
        assert get_effective_strategy(_rs(), now=_at(21, 59))[1] is False
        assert get_effective_strategy(_rs(), now=_at(6, 59))[1] is True
        assert get_effective_strategy(_rs(), now=_at(7, 0))[1] is False


class TestAWindowInsideOneDay:
    """The other shape: 09:00-17:00 never crosses midnight."""

    def _rs(self):
        return _rs(ooh_start_time="09:00", ooh_end_time="17:00")

    @pytest.mark.parametrize("hh", [9, 12, 16])
    def test_inside(self, hh):
        assert get_effective_strategy(self._rs(), now=_at(hh))[1] is True

    @pytest.mark.parametrize("hh", [8, 17, 23, 2])
    def test_outside(self, hh):
        assert get_effective_strategy(self._rs(), now=_at(hh))[1] is False


class TestTheOptionalDateRange:
    """Holidays: OOH applies all day, but only on dates in the range."""

    def _rs(self, **over):
        return _rs(ooh_date_active=1, ooh_date_from="2026-06-10",
                   ooh_date_to="2026-06-20", **over)

    def test_inside_the_range_the_time_window_still_applies(self):
        """The range narrows WHEN OOH can apply; it does not make it all-day
        on its own."""
        assert get_effective_strategy(self._rs(), now=_at(2, d=(2026, 6, 15)))[1] is True
        assert get_effective_strategy(self._rs(), now=_at(12, d=(2026, 6, 15)))[1] is False

    def test_outside_the_range_ooh_never_applies(self):
        assert get_effective_strategy(self._rs(), now=_at(2, d=(2026, 7, 1)))[1] is False

    def test_the_range_ends_are_inclusive(self):
        assert get_effective_strategy(self._rs(), now=_at(2, d=(2026, 6, 10)))[1] is True
        assert get_effective_strategy(self._rs(), now=_at(2, d=(2026, 6, 20)))[1] is True
        assert get_effective_strategy(self._rs(), now=_at(2, d=(2026, 6, 21)))[1] is False

    def test_the_range_is_ignored_when_not_switched_on(self):
        rs = _rs(ooh_date_active=0, ooh_date_from="2026-01-01",
                 ooh_date_to="2026-01-02")

        assert get_effective_strategy(rs, now=_at(2, d=(2026, 6, 15)))[1] is True

    def test_a_half_filled_range_is_ignored_rather_than_guessed(self):
        rs = _rs(ooh_date_active=1, ooh_date_from="2026-06-10", ooh_date_to="")

        assert get_effective_strategy(rs, now=_at(2, d=(2026, 6, 15)))[1] is True


class TestBadConfigurationFallsBackToTheBaseStrategy:
    """Every one of these could otherwise raise inside the path that opens a
    trade. Falling back to the configured strategy is the conservative answer:
    it is what the operator chose."""

    @pytest.mark.parametrize("bad", [
        {"ooh_start_time": "not-a-time"},
        {"ooh_end_time": "25:99:99"},
        {"ooh_date_active": 1, "ooh_date_from": "nonsense",
         "ooh_date_to": "2026-06-20"},
        {"ooh_date_active": 1, "ooh_date_from": "2026-06-10",
         "ooh_date_to": "2026-13-45"},
    ])
    def test_it_returns_the_base_strategy(self, bad):
        assert get_effective_strategy(_rs(**bad), now=_at(2)) == \
            ("scale_out", False)

    def test_an_EMPTY_time_falls_back_to_the_default_window(self):
        """Not bad configuration -- `rs.get(...) or "22:00"` is deliberate, so
        a blank field means the default rather than an error. My first draft of
        this test had it in the list above and was wrong about the code."""
        assert get_effective_strategy(_rs(ooh_start_time=""), now=_at(2)) == \
            ("conservative", True)
        assert get_effective_strategy(_rs(ooh_start_time=""), now=_at(12))[1] is False


class TestTheClock:
    def test_out_of_hours_is_evaluated_in_UTC(self):
        """Asserted rather than assumed. The Trading Schedule moved to UK time
        on 2026-09-01 and this did not; if that is ever revisited, this test is
        where the decision is recorded. See docs/simon-handover/020."""
        summer_utc_2am = datetime(2026, 6, 15, 2, 0, tzinfo=timezone.utc)

        # 02:00 UTC is 03:00 UK in summer. Both are inside 22:00-07:00, so the
        # result alone cannot tell them apart -- 07:30 UK / 06:30 UTC can.
        assert get_effective_strategy(
            _rs(), now=datetime(2026, 6, 15, 6, 30, tzinfo=timezone.utc))[1] is True
        assert get_effective_strategy(_rs(), now=summer_utc_2am)[1] is True

    def test_it_still_reads_the_clock_when_no_time_is_given(self):
        """The `now` parameter exists for tests; production passes nothing."""
        strat, active = get_effective_strategy(_rs())

        assert strat in ("scale_out", "conservative")
        assert isinstance(active, bool)
