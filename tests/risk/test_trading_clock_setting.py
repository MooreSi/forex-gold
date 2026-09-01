"""Setting the trading clock, and reading back what it currently is.

The offset column and every reader of it went in first; nothing set it. These
cover the write side and the one-line summary the UI shows, because a clock
control that cannot tell you what time it thinks it is is worse than none.
"""
from datetime import datetime, timezone

import pytest

from backend.src.services.risk import clock as risk_clock
from backend.src.utils.trading_clock import SETTING_KEY


@pytest.fixture
def store(monkeypatch):
    """A settings dict standing in for vantage_risk_settings."""
    rs: dict = {}
    written: list[dict] = []

    def _update(updates, _from_sync=False):
        written.append(dict(updates))
        rs.update(updates)
        return rs

    monkeypatch.setattr(risk_clock, "_rs", lambda: dict(rs))
    monkeypatch.setattr(
        "backend.src.services.risk.risk_settings_repo.update_risk_settings",
        _update,
    )
    return rs, written


class TestSettingIt:
    def test_an_offset_is_stored(self, store):
        rs, written = store
        risk_clock.set_offset_minutes(60)

        assert rs[SETTING_KEY] == 60
        assert written == [{SETTING_KEY: 60}]

    def test_zero_is_a_real_offset_not_an_absence(self, store):
        """UTC+0 is a place people live. It must not be read as "unset"."""
        rs, _ = store
        risk_clock.set_offset_minutes(0)

        assert rs[SETTING_KEY] == 0
        assert risk_clock.offset_minutes() == 0

    def test_none_clears_it_back_to_the_machine_clock(self, store):
        rs, written = store
        risk_clock.set_offset_minutes(-300)
        risk_clock.set_offset_minutes(None)

        assert rs[SETTING_KEY] is None
        assert written[-1] == {SETTING_KEY: None}
        assert risk_clock.offset_minutes() is None

    def test_a_negative_offset_is_kept(self, store):
        """Half the world is west of the meridian."""
        rs, _ = store
        risk_clock.set_offset_minutes(-330)

        assert risk_clock.offset_minutes() == -330

    def test_a_wild_offset_is_refused(self, store):
        """More than a day out is a typo, not a timezone. Refusing at the
        setter means the reader never has to decide what to do with it."""
        rs, written = store
        with pytest.raises(ValueError):
            risk_clock.set_offset_minutes(4000)

        assert written == [], "a nonsense offset reached the database"

    def test_a_non_number_is_refused(self, store):
        _, written = store
        with pytest.raises(ValueError):
            risk_clock.set_offset_minutes("half past")

        assert written == []

    def test_a_float_that_is_a_whole_number_of_minutes_is_accepted(self, store):
        """The UI's number input hands back floats."""
        rs, _ = store
        risk_clock.set_offset_minutes(60.0)

        assert rs[SETTING_KEY] == 60
        assert isinstance(rs[SETTING_KEY], int)


class TestDescribingIt:
    def test_it_reports_the_machine_clock_when_nothing_is_set(self, store):
        desc = risk_clock.describe()

        assert desc["configured"] is None
        assert desc["following_machine"] is True

    def test_it_reports_a_configured_offset(self, store):
        risk_clock.set_offset_minutes(330)
        desc = risk_clock.describe()

        assert desc["configured"] == 330
        assert desc["following_machine"] is False
        assert desc["effective"] == 330

    def test_the_label_is_readable(self, store):
        risk_clock.set_offset_minutes(330)

        assert risk_clock.describe()["label"] == "UTC+05:30"

    def test_the_label_handles_a_negative_half_hour(self, store):
        """-210 is UTC-03:30, not UTC-3:-30 and not UTC-2:30."""
        risk_clock.set_offset_minutes(-210)

        assert risk_clock.describe()["label"] == "UTC-03:30"

    def test_utc_is_labelled_utc(self, store):
        risk_clock.set_offset_minutes(0)

        assert risk_clock.describe()["label"] == "UTC+00:00"

    def test_the_time_it_reports_is_the_time_the_gate_will_use(self, store):
        """The summary and the schedule gate must not be able to disagree --
        that is the whole point of showing it."""
        risk_clock.set_offset_minutes(120)
        desc = risk_clock.describe()

        assert abs((desc["now"] - risk_clock.now()).total_seconds()) < 2

    def test_the_reported_time_actually_moves_with_the_offset(self, store):
        """A summary computed from the machine clock regardless of the setting
        would pass every test above and be wrong on the machine that needs it."""
        risk_clock.set_offset_minutes(0)
        at_utc = risk_clock.describe()["now"]
        risk_clock.set_offset_minutes(180)
        at_plus_three = risk_clock.describe()["now"]

        assert round((at_plus_three - at_utc).total_seconds() / 3600) == 3

    def test_it_survives_a_settings_read_failure(self, monkeypatch):
        """describe() runs on a page render. A dead database should show the
        machine's clock, not a stack trace where the tab should be."""
        def _boom():
            raise RuntimeError("no database")
        monkeypatch.setattr(
            "backend.src.services.risk.risk_settings_repo.get_risk_settings",
            _boom,
        )

        desc = risk_clock.describe()

        assert desc["following_machine"] is True
        assert isinstance(desc["now"], datetime)


class TestTheControllerExposesIt:
    def test_it_forwards_the_setter(self, store, monkeypatch):
        from backend.src.controllers import schedule_controller as ctl
        seen = []
        monkeypatch.setattr(risk_clock, "set_offset_minutes", seen.append)

        ctl.set_trading_clock_offset(60)

        assert seen == [60]

    def test_it_forwards_the_summary(self, store):
        from backend.src.controllers import schedule_controller as ctl

        assert ctl.describe_trading_clock()["following_machine"] is True
