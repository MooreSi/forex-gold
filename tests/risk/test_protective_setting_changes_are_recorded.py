"""A change to a protective limit leaves a trace.

On 2026-09-02 the owner's governor was off, his drawdown limit was 40% and his
daily-loss limit 20%. The day before, at my prompting, he had set them to on,
10% and 3%. Establishing which of three things had happened —

  * he changed them back himself,
  * the cross-node settings sync pushed the VPS's older values over them, or
  * something else wrote them —

took a database query, a check of `_SYNCED_SETTINGS_KEYS` (all four ARE
synced), a search for a sync connection (there is none configured) and two log
greps that found nothing at all.

It was benign: he changed them. But the settings that decide **when trading
stops** changed and nothing recorded it, and "was that me or the other node?"
is not a question anyone should have to answer by inference.

So the protective ones are logged when they move, with the old value, the new
value and whether it arrived over the sync channel. The rest stay quiet — this
runs on every settings save, and a line per key per save is the noise problem
already fixed three times in this codebase.
"""
from __future__ import annotations

import logging

import pytest

from backend.src.services.risk import risk_settings_repo as repo


@pytest.fixture
def settings(fresh_db):
    return repo


class TestAProtectiveLimitMoving:
    def test_it_is_logged_at_warning(self, settings, caplog):
        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({"max_total_drawdown_pct": 40.0})

        assert "max_total_drawdown_pct" in caplog.text

    def test_the_old_and_new_values_are_both_there(self, settings, caplog):
        """"It is 40 now" does not answer "what was it before". Without the
        old value the log cannot tell a tightening from a loosening."""
        repo.update_risk_settings({"max_total_drawdown_pct": 10.0})

        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({"max_total_drawdown_pct": 40.0})

        assert "10" in caplog.text and "40" in caplog.text

    @pytest.mark.parametrize("key,before,after", [
        ("risk_governor_enabled", 1, 0),
        ("max_total_drawdown_pct", 10.0, 40.0),
        ("max_daily_loss_pct", 3.0, 20.0),
        ("circuit_breaker_enabled", 1, 0),
        ("max_open_trades", 5, 20),
    ])
    def test_every_protective_setting_is_covered(self, settings, caplog,
                                                 key, before, after):
        """Each is moved from a deliberate starting value: two of these
        already sit at the value being set in a fresh database, so setting
        them again is not a change and correctly logs nothing."""
        repo.update_risk_settings({key: before})

        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({key: after})

        assert key in caplog.text

    def test_it_says_whether_the_change_came_over_the_sync_channel(
        self, settings, caplog,
    ):
        """The question that started this. All four protective limits are in
        _SYNCED_SETTINGS_KEYS, so the other node can move them."""
        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({"max_daily_loss_pct": 20.0}, _from_sync=True)

        assert "sync" in caplog.text.lower()

    def test_a_local_edit_is_not_labelled_as_a_sync(self, settings, caplog):
        """Negative control: labelling everything "from sync" would answer the
        question wrongly rather than not at all.

        The value must actually MOVE. An earlier version set
        max_daily_loss_pct to 3.0, which is already the schema default, so
        nothing changed, nothing was logged, and the test passed with the
        origin hard-coded to "from sync". Mutation found it.
        """
        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({"max_daily_loss_pct": 12.5})

        assert "max_daily_loss_pct" in caplog.text
        assert "sync" not in caplog.text.lower()


class TestSettingIt_To_The_Same_Value:
    def test_it_is_not_logged(self, settings, caplog):
        """Saving the settings page rewrites every field. Logging unchanged
        ones turns one save into a wall of warnings and buries the real
        change among them."""
        repo.update_risk_settings({"max_daily_loss_pct": 3.0})

        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({"max_daily_loss_pct": 3.0})

        assert "max_daily_loss_pct" not in caplog.text


class TestEverythingElse:
    def test_an_ordinary_setting_does_not_warn(self, settings, caplog):
        """`profit_close_usd` is a preference, not a protection. This runs on
        every save; only the settings that stop trading are worth a warning."""
        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({"profit_close_usd": 5.0})

        assert caplog.text.strip() == ""

    def test_a_mixed_save_logs_only_the_protective_one(self, settings, caplog):
        with caplog.at_level(logging.WARNING):
            repo.update_risk_settings({"profit_close_usd": 5.0,
                                       "max_daily_loss_pct": 20.0})

        assert "max_daily_loss_pct" in caplog.text
        assert "profit_close_usd" not in caplog.text


class TestItCannotBreakTheSave:
    def test_a_failure_to_read_the_old_values_does_not_stop_the_write(
        self, settings, monkeypatch,
    ):
        """The log is a nicety; the setting is not. A settings save must never
        fail because its audit line could not be built."""
        calls = {"n": 0}
        real = repo.get_risk_settings

        def _flaky():
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("no database")
            return real()
        monkeypatch.setattr(repo, "get_risk_settings", _flaky)

        repo.update_risk_settings({"max_daily_loss_pct": 7.0})

        assert real()["max_daily_loss_pct"] == 7.0
