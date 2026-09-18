"""Recording a Telegram execution decision (stage 0).

Expected behaviour, from docs/todo/signal-validation/010:

  * Off by default and completely inert when off. This sits on the order
    path; an install that has not asked for it must not pay for it.
  * It NEVER raises. A research log that can break signal execution is worse
    than no research log -- core_signal_snapshot polls rather than hooks for
    exactly this reason, and this one cannot poll because the thing it is
    recording is a decision, not a row.
  * The shadow facts are gathered BEFORE any gate returns, so every row
    carries the same fact set whichever gate decided it. Recording at each
    early return would log "would take" for a variant whose later gates
    never ran -- reversal_engine_live_execute.py's rule, learned there.
  * The facts are computed with the gates forced ON regardless of the live
    toggles. A shadow of a gate that is off is the entire point.
"""
import os
import tempfile
import time
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.services.signals import decision_log as dlog
from backend.src.services.signals import decision_log_repo as repo
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from tests.conftest import remove_db_file


@pytest.fixture
def log_db():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    repo.create_schema()
    yield repo
    re_repo.close_db()
    remove_db_file(path)


ON = {"tg_decision_log_enabled": 1}
TICK = SimpleNamespace(bid=4101.0, ask=4101.3, spread_points=30.0)


def _record(rs, **over):
    kwargs = dict(
        rs=rs, tg_id="21429", path="auto", channel_name="Gold Diggers VIP",
        direction="BUY", executed=True, skip_reason="", strategy="scale_out",
        parsed={"entry_low": 4100.0, "entry_high": 4102.0,
                "stop_loss": 4090.0, "tp1": 4120.0},
        tick=TICK, trade_id="trade-1", facts={"liquidity_blocked": False,
                                              "event_blocked": False},
    )
    kwargs.update(over)
    return dlog.record(**kwargs)


class TestTheToggleIsTheWholeSwitch:
    def test_nothing_is_written_when_it_is_off(self, log_db):
        assert _record({"tg_decision_log_enabled": 0}) is None
        assert repo.rows() == []

    def test_it_is_off_when_the_key_is_absent(self, log_db):
        """Default off. A new column on an existing install reads as 0, and
        an install that never opened the page must stay inert."""
        assert _record({}) is None
        assert repo.rows() == []

    def test_a_decision_is_written_when_it_is_on(self, log_db):
        assert _record(ON) is not None
        rows = repo.rows()
        assert len(rows) == 1
        assert rows[0]["tg_message_id"] == "21429"
        assert rows[0]["executed"] == 1
        assert rows[0]["trade_id"] == "trade-1"


class TestItNeverRaises:
    def test_a_broken_repo_does_not_reach_the_caller(self, log_db):
        """The order path calls this. It must swallow everything."""
        with mock.patch.object(repo, "insert_decision",
                               side_effect=RuntimeError("db is on fire")):
            assert _record(ON) is None

    def test_a_broken_settings_read_does_not_reach_the_caller(self, log_db):
        class Exploding(dict):
            def get(self, *a, **k):
                raise RuntimeError("no settings")

        assert _record(Exploding()) is None

    def test_a_missing_tick_is_recorded_not_refused(self, log_db):
        """No live price is a fact about the decision, not a reason to lose
        the row -- it is one of the reasons a trade gets blocked."""
        assert _record(ON, tick=None, executed=False,
                       skip_reason="no live price", trade_id=None) is not None
        row = repo.rows()[0]
        assert row["bid"] is None
        assert row["executed"] == 0

    def test_a_signal_with_no_levels_is_recorded(self, log_db):
        """IME decides on a bare direction. entry/SL/TP are simply absent,
        and that path is the one most in need of measuring."""
        assert _record(ON, path="ime", parsed=None) is not None
        row = repo.rows()[0]
        assert row["path"] == "ime"
        assert row["entry_low"] is None
        assert row["direction"] == "BUY"


class TestTheInlineFacts:
    def test_the_liquidity_fact_ignores_the_LIVE_toggle(self):
        """session_liquidity_gate_enabled is OFF here. A shadow that only
        evaluated gates already switched on could never tell you whether to
        switch one on."""
        sunday_reopen = 1789335000.0  # Sun 2026-09-13 21:30 UTC, inside the settle window
        facts = dlog.inline_facts({"session_liquidity_gate_enabled": 0},
                                  now_ts=sunday_reopen, tick=TICK, events=[])
        assert facts["liquidity_blocked"] is True

    def test_an_ordinary_moment_is_not_blocked(self):
        thursday_noon = 1789646400.0   # Thu 2026-09-17 12:00 UTC
        facts = dlog.inline_facts({}, now_ts=thursday_noon, tick=TICK, events=[])
        assert facts["liquidity_blocked"] is False
        assert facts["event_blocked"] is False

    def test_a_tier_one_event_inside_its_window_blocks(self):
        facts = dlog.inline_facts(
            {}, now_ts=1789646400.0, tick=TICK,
            events=[{"title": "FOMC Rate Decision", "impact": "High",
                     "mins_until": 3.0}])
        assert facts["event_blocked"] is True

    def test_the_spread_is_carried_through(self):
        facts = dlog.inline_facts({}, now_ts=1789646400.0, tick=TICK, events=[])
        assert facts["spread_points"] == 30.0

    def test_an_unavailable_fact_is_None_and_never_False(self):
        """None means "not known". False would be read as "checked, fine"."""
        facts = dlog.inline_facts({}, now_ts=1789646400.0, tick=None, events=[])
        assert facts["spread_points"] is None

    def test_a_broken_calendar_abstains_instead_of_raising(self):
        with mock.patch.object(dlog, "_events_now", side_effect=RuntimeError("feed down")):
            facts = dlog.inline_facts({}, now_ts=1789646400.0, tick=TICK)
        assert facts["event_blocked"] is None
        assert facts["liquidity_blocked"] is False, "one broken fact must not lose the others"


class TestTheHelpersActuallyResolve:
    """Both helpers swallow exceptions, which is right on the order path and
    also the perfect place to hide a misremembered API: a wrong call would
    return None forever and every other test here would still pass. These
    two assert the real value comes back."""

    def test_the_account_environment_is_recorded(self, log_db):
        _record(ON)
        assert repo.rows()[0]["account_env"] in ("demo", "live")

    def test_the_session_is_recorded(self, log_db):
        _record(ON)
        assert repo.rows()[0]["session"] in (
            "asian", "london", "overlap", "ny", "off", "closed")
