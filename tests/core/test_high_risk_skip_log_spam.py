"""An excluded high-risk message must not re-log on every scan cycle.

bugs/065. Exactly the shape of bugs/015 (the bare-direction line), in the
sibling branch nobody checked at the time: `exclude_high_risk` drops the
message with `continue` before the dedup lookup that would have marked it
seen, so the scan loop handles it again about once a second for as long as
it stays in the reader's fetch window.

Observed live on 2026-09-18: 204,359 of the day's 255,588 log lines -- 80%
of the file, 31MB by 07:28 -- were this one line, cycling over nine
messages, the oldest of which (tg_id 1627) was from a previous session.
The grep in the runbook that finds real faults returns this and nothing
else.

This covers the LOG half only, deliberately and for the same reason 015
did: marking the message processed would change which signals the parser
sees on a later cycle, and that is a trading-behaviour change the owner
decides. What is fixed here is that the operator sees the skip once, which
is what the line is for.
"""
from __future__ import annotations

import asyncio
import logging
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.signals import scan_messages as sm
from backend.src.services.telegram import alerts as telegram_alerts


_HIGH_RISK = (
    "BUY LIMITS GOLD @ 4378/4372 AREA\n\nTP 4381\nTP 4385\nSL 4371\n\n"
    "HIGH RISK TRADE"
)


class _FakeTgReader:
    def __init__(self, messages):
        self._messages = messages

    def get_buffer_messages(self, limit=100):
        return self._messages

    def get_active_group_slots(self):
        return {}

    def get_group_name(self, group_id):
        return "GOLD DIGGERS INSTITUTIONAL"


def _scan(engine):
    with mock.patch.object(db, "should_generate_signals_here", return_value=True), \
         mock.patch.object(telegram_alerts, "send_message", new=mock.AsyncMock()):
        return asyncio.run(engine._scan_messages())


def _engine(make_engine, msgs):
    return make_engine(
        _tg_reader=_FakeTgReader(msgs), _cfg={}, _bridge=None,
        _dpm_candles=None, _tg_off_warn_state={},
    )


def _msg(tg_id, text=_HIGH_RISK):
    return {"id": tg_id, "group_id": "g1", "text": text, "timestamp": ""}


def _skip_lines(caplog):
    return [r for r in caplog.records if "Skipping high-risk signal" in r.getMessage()]


@pytest.fixture(autouse=True)
def clean_seen():
    """The suppression is module state, so it must not leak between tests."""
    sm.reset_high_risk_log_memory()
    yield
    sm.reset_high_risk_log_memory()


@pytest.fixture
def excluding(fresh_db):
    db.update_risk_settings({"accept_tg_signals": 1, "exclude_high_risk": 1})
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    return fresh_db


class TestItLogsOnce:
    def test_the_first_sighting_is_logged(self, excluding, make_engine, caplog):
        engine = _engine(make_engine, [_msg("30374")])
        with caplog.at_level(logging.INFO, logger=sm.log.name):
            assert _scan(engine) == []

        assert len(_skip_lines(caplog)) == 1

    def test_RESCANNING_THE_SAME_MESSAGE_IS_SILENT(self, excluding, make_engine, caplog):
        """The bug. Nine messages producing 204,359 lines in one day."""
        engine = _engine(make_engine, [_msg("30374")])
        with caplog.at_level(logging.INFO, logger=sm.log.name):
            for _ in range(50):
                _scan(engine)

        assert len(_skip_lines(caplog)) == 1

    def test_A_DIFFERENT_MESSAGE_IS_STILL_LOGGED(self, excluding, make_engine, caplog):
        """Suppression must be per message. Silencing the second one would
        hide a real high-risk signal arriving after the first."""
        engine = _engine(make_engine, [_msg("30374"), _msg("30368")])
        with caplog.at_level(logging.INFO, logger=sm.log.name):
            _scan(engine)
            _scan(engine)

        assert len(_skip_lines(caplog)) == 2

    def test_AN_EDIT_THAT_IS_STILL_HIGH_RISK_IS_LOGGED_AGAIN(self, excluding, make_engine, caplog):
        """Keyed on the body, not the id alone. An edited message is a
        different decision on different text, and the operator needs to see
        that the edit was dropped too."""
        engine = _engine(make_engine, [_msg("30374")])
        with caplog.at_level(logging.INFO, logger=sm.log.name):
            _scan(engine)
            edited = _engine(make_engine, [_msg("30374", _HIGH_RISK + "\nENTRY MOVED TO 4380")])
            _scan(edited)

        assert len(_skip_lines(caplog)) == 2

    def test_the_line_still_names_the_message(self, excluding, make_engine, caplog):
        """It is the only trace this message left. Losing the id would make
        it untraceable."""
        engine = _engine(make_engine, [_msg("30374")])
        with caplog.at_level(logging.INFO, logger=sm.log.name):
            _scan(engine)

        assert "30374" in _skip_lines(caplog)[0].getMessage()


class TestTheSkipItselfIsUNCHANGED:
    def test_the_message_is_still_skipped_on_every_cycle(self, excluding, make_engine, caplog):
        """Behaviour is unchanged: still dropped, still no signal, on the
        silent cycles as much as the logged one. Only the logging differs."""
        engine = _engine(make_engine, [_msg("30374")])
        for _ in range(3):
            assert _scan(engine) == []

        with db.db() as conn:
            assert conn.execute("SELECT COUNT(*) FROM vantage_tg_signals").fetchone()[0] == 0

    def test_with_the_setting_OFF_nothing_is_skipped_or_suppressed(self, fresh_db, make_engine, caplog):
        """The suppression must not leak into the setting-off path: with
        exclude_high_risk=0 the message goes to the parser as before."""
        db.update_risk_settings({"accept_tg_signals": 1, "exclude_high_risk": 0})
        db._rs_cache = None
        db._rs_cache_ts = 0.0
        engine = _engine(make_engine, [_msg("30374")])
        with caplog.at_level(logging.INFO, logger=sm.log.name):
            _scan(engine)

        assert _skip_lines(caplog) == []


class TestTheMemoryIsBounded:
    def test_it_does_not_grow_without_limit(self, excluding, make_engine, caplog):
        """It is module state in a process that runs for weeks. An unbounded
        record of every high-risk message ever seen is a slow leak."""
        engine = _engine(make_engine, [_msg(str(i)) for i in range(sm._HIGH_RISK_LOG_MEMORY + 200)])
        with caplog.at_level(logging.INFO, logger=sm.log.name):
            _scan(engine)

        assert len(sm._high_risk_logged) <= sm._HIGH_RISK_LOG_MEMORY

    def test_the_MOST_RECENT_message_is_still_remembered(self, excluding, make_engine, caplog):
        """Eviction must drop the oldest. Dropping the newest would restore
        the every-cycle spam for the message currently in the window --
        exactly the case this exists for."""
        newest = str(sm._HIGH_RISK_LOG_MEMORY + 199)
        engine = _engine(make_engine, [_msg(str(i)) for i in range(sm._HIGH_RISK_LOG_MEMORY + 200)])
        _scan(engine)

        with caplog.at_level(logging.INFO, logger=sm.log.name):
            _scan(_engine(make_engine, [_msg(newest)]))

        assert _skip_lines(caplog) == []
