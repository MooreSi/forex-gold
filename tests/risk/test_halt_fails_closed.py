"""If the app cannot tell whether trading is halted, it must not trade.

`is_trading_paused()` is the last line of defence. `open_trade` checks it
before both send paths -- the EA handoff and the Python bridge -- and
`tests/refactor/test_order_paths_have_one_funnel.py` pins that ordering.

But the check reads `trade_pause_until` through `get_app_config`, which
swallows every database error and returns `None`. `None` is also what an unset
key returns, and unset means "not paused". So:

    a locked or failing database  ->  get_app_config returns None
                                  ->  "not paused"
                                  ->  the halt does not apply

A protective halt that stops protecting on a transient read error is worse than
no halt, because the operator believes one is in place. SQLite reads here run
with a 5s busy timeout and this repo has a recorded history of lock storms, so
this is not hypothetical.

Both failure directions are covered. Failing closed on a read error is right;
failing closed on an *unset* key would refuse to trade on a healthy install
that has simply never been paused.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.risk import governor
from backend.src.services.risk import app_config_repo


class TestAnUnreadablePauseStateMeansPAUSED:

    def test_a_database_error_reports_paused(self, monkeypatch, caplog):
        def _boom(key):
            raise RuntimeError("database is locked")
        monkeypatch.setattr(app_config_repo, "read_app_config_strict", _boom)

        with caplog.at_level("ERROR"):
            assert governor.is_trading_paused() is True, (
                "a failed read reported 'not paused' -- a halted account would "
                "resume trading on a transient database error"
            )
        assert any("PAUSED" in r.getMessage() for r in caplog.records), (
            "it failed closed silently; the operator needs to know why trading "
            "stopped"
        )

    def test_a_MALFORMED_value_reports_paused(self, monkeypatch, caplog):
        """Garbage in `trade_pause_until` means the pause window is unknowable.
        Resuming is a guess; refusing is not."""
        monkeypatch.setattr(app_config_repo, "read_app_config_strict",
                            lambda key: "not-a-timestamp")

        with caplog.at_level("ERROR"):
            assert governor.is_trading_paused() is True


class TestTheOrdinaryCasesAreUnchanged:
    """The control that matters: failing closed must not mean refusing to trade
    on a healthy install."""

    def test_an_unset_key_is_NOT_paused(self, monkeypatch):
        monkeypatch.setattr(app_config_repo, "read_app_config_strict",
                            lambda key: None)

        assert governor.is_trading_paused() is False

    def test_an_empty_value_is_NOT_paused(self, monkeypatch):
        monkeypatch.setattr(app_config_repo, "read_app_config_strict",
                            lambda key: "")

        assert governor.is_trading_paused() is False

    def test_an_EXPIRED_pause_is_not_paused(self, monkeypatch):
        monkeypatch.setattr(app_config_repo, "read_app_config_strict",
                            lambda key: str(time.time() - 60))

        assert governor.is_trading_paused() is False

    def test_a_LIVE_pause_is_paused(self, monkeypatch):
        monkeypatch.setattr(app_config_repo, "read_app_config_strict",
                            lambda key: str(time.time() + 3600))

        assert governor.is_trading_paused() is True

    def test_a_zero_is_not_paused(self, monkeypatch):
        """`_resume_trading` writes "0" to clear a halt."""
        monkeypatch.setattr(app_config_repo, "read_app_config_strict",
                            lambda key: "0")

        assert governor.is_trading_paused() is False


class TestTheStrictReadDoesNotSwallow:
    """The whole fix rests on having a read that reports failure. If it
    swallowed like its sibling, the guard above could never fire."""

    def test_it_raises_rather_than_returning_None(self, monkeypatch):
        import backend.src.services.risk.app_config_repo as repo

        def _boom():
            raise RuntimeError("database is locked")
        monkeypatch.setattr(repo, "db", _boom)

        with pytest.raises(Exception):
            repo.read_app_config_strict("trade_pause_until")

    def test_the_lenient_one_still_swallows(self, monkeypatch):
        """Unchanged on purpose: 40-odd callers read optional configuration
        through it and expect None rather than an exception."""
        import backend.src.services.risk.app_config_repo as repo

        def _boom():
            raise RuntimeError("database is locked")
        monkeypatch.setattr(repo, "db", _boom)

        assert repo.get_app_config("anything") is None
