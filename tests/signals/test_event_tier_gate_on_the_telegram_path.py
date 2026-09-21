"""Stage 2: the event-tier gate, promoted onto the Telegram execution path.

**No test here reaches a broker.** The IME path is driven with `bridge=None`
and the gate under test sits above the first call that would touch one, so a
blocked decision never reaches `get_tick`. Nothing places, closes or modifies
an order, on any account.

WHY THIS GATE, AND ONLY THIS ONE
--------------------------------
`docs/todo/signal-validation/000` section "Stage 2" says promote one at a
time, because a stack promoted together cannot be attributed when the number
moves. The decision log's champion/challenger record, read 2026-09-21 over
322 scored decisions, is what chose it:

    variant           scored  skipped  loss avoided  kept
    confirmed entry      322      320    -$4,072.84  +$58.58
    event tier            39        4      -$219.50  -$650.46
    trend (HTF bias)      39        9      -$189.94  -$680.02
    session liquidity    322        0          $0.00  -$4,014.26
    spread guard         322        0          $0.00  -$4,014.26

`confirmed entry` refuses 99.4% of signals. That is not a filter, it is a
verdict on the channel, and the spec says so in as many words. Session
liquidity and the spread guard changed nothing at all on this corpus. Of what
is left, the event tier removed a quarter of the loss on the rows where it
had an opinion by standing aside from a tenth of the trades -- the only
favourable ratio here that is not a blanket refusal.

**n=39 is small and this gate is armed by the owner for a demo session, not
shipped on.** `tg_event_tier_gate_enabled` defaults to 0 (migration 50).

WHY A TELEGRAM-SPECIFIC SWITCH
-----------------------------
`event_tier_gate_enabled` already exists and the Reversal Engine's live path
reads it through `capability_gates.liquidity_blocks`. Reusing it would arm
two engines from one switch, which destroys the attribution the whole staged
plan is built on. So the Telegram path gets its own column, and the tier
windows themselves stay in `event_tiers.Config` -- one definition of what a
tier-1 window is, two independent decisions about who honours it.
"""
from __future__ import annotations

import asyncio
import inspect
import os
import tempfile
from datetime import datetime, timezone
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.risk import capability_gates as caps
from backend.src.services.signals import decision_log_repo as repo
from backend.src.services.trading import instant_entry
from tests.conftest import remove_db_file


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _fomc(mins_until: float) -> dict:
    """A tier-1 event. Tier 1 needs HIGH impact AND a tier-1 keyword -- a
    "Fed Member Speaks" footnote must not become a full FOMC blackout."""
    return {"title": "FOMC Statement", "impact": "high",
            "mins_until": mins_until, "ts": 0.0}


def _minor(mins_until: float) -> dict:
    return {"title": "Trade Balance", "impact": "low",
            "mins_until": mins_until, "ts": 0.0}


ON = {"tg_event_tier_gate_enabled": 1}
OFF: dict = {}


class TestTheGateItself:
    def test_a_default_settings_row_blocks_nothing(self):
        assert caps.tg_event_tier_blocks(OFF, events=[_fomc(5)]) is None

    def test_a_settings_row_missing_the_column_behaves_the_same(self):
        """A client that has not run migration 50 must trade exactly as it
        did -- `capability_gates`' own load-bearing property."""
        assert caps.tg_event_tier_blocks({"trade_strategy": "scale_out"},
                                         events=[_fomc(5)]) is None

    def test_off_does_not_even_read_the_calendar(self):
        """The negative control for the two above. `get_events` can reach the
        network; a feed call on the order path for a gate nobody enabled is a
        cost with no benefit, and a gate that reads first and checks the
        switch afterwards is indistinguishable from one that is on."""
        with mock.patch("backend.src.utils.news_calendar.get_events",
                        side_effect=AssertionError("read the calendar while off")):
            assert caps.tg_event_tier_blocks(OFF) is None

    def test_on_and_a_tier_one_event_is_near_stands_aside(self):
        reason = caps.tg_event_tier_blocks(ON, events=[_fomc(5)])
        assert reason is not None
        assert "FOMC" in reason

    def test_on_and_the_window_has_passed_does_not(self):
        # Tier 1 is 60 before / 90 after. Two hours after is outside both.
        assert caps.tg_event_tier_blocks(ON, events=[_fomc(-120)]) is None

    def test_on_with_nothing_on_the_calendar_does_not(self):
        assert caps.tg_event_tier_blocks(ON, events=[]) is None

    def test_a_minor_event_is_not_a_tier_one_blackout(self):
        """Tier 3 is 10 before / 20 after, so a low-impact print 30 minutes
        out must not stop anything."""
        assert caps.tg_event_tier_blocks(ON, events=[_minor(30)]) is None

    def test_a_calendar_that_cannot_be_read_does_not_block(self):
        """A feed that is down has not said no. Fail open, in the same
        direction `upcoming_events` already fails and the same direction the
        shadow log ABSTAINS rather than refusing -- otherwise a broken feed
        silently halts trading and nothing on any screen says why."""
        with mock.patch("backend.src.utils.news_calendar.get_events",
                        side_effect=RuntimeError("feed down")):
            assert caps.tg_event_tier_blocks(ON) is None


class TestItDoesNotArmTheReversalEngine:
    """The attribution rule, as a test. Two engines armed by one switch
    cannot be told apart when the number moves."""

    def test_the_tg_switch_leaves_the_engine_gate_off(self):
        assert caps.liquidity_blocks(1_788_998_400.0, ON, events=[_fomc(5)]) is None

    def test_and_the_engine_switch_leaves_the_tg_gate_off(self):
        assert caps.tg_event_tier_blocks({"event_tier_gate_enabled": 1},
                                         events=[_fomc(5)]) is None


class TestBothTelegramOrderPathsConsultIt:
    """Source inspection, because the defect this is guarding against is a
    gate that reaches one path and not the other -- which is exactly how the
    schedule gate and the news blackout each needed a second copy here."""

    @pytest.mark.parametrize("module_path,fn_name", [
        ("backend.src.services.signals.resolution", "resolve_open_trade_params"),
        ("backend.src.services.trading.instant_entry", "process_instant_entry"),
    ])
    def test_it_is_called(self, module_path, fn_name):
        import importlib
        mod = importlib.import_module(module_path)
        assert "tg_event_tier_blocks" in inspect.getsource(getattr(mod, fn_name)), (
            f"{module_path}.{fn_name} places an order without the event-tier "
            f"gate -- the gate then covers one Telegram path and not the other"
        )


@pytest.fixture
def both_dbs(fresh_db):
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    re_repo.init(path)
    repo.create_schema()
    db.update_risk_settings({"tg_decision_log_enabled": 1,
                             "accept_tg_signals": 1, "max_open_trades": 5})
    db._rs_cache = None
    db._rs_cache_ts = 0.0
    yield db
    re_repo.close_db()
    remove_db_file(path)


class TestTheIMEPathReallyRunsIt:
    """The wiring tests above read source; this one runs it. Both exist on
    purpose -- `docs/todo/refactor` records a guardrail that scanned a deleted
    directory and printed "all good" every run for months.

    `bridge=None` throughout. The gate sits above the first call that would
    touch a broker, so a blocked decision never reaches one; a gate wired
    BELOW that call would fail these tests with an AttributeError rather than
    passing quietly, which is the point.
    """

    def _run(self, rs, bridge=None):
        return asyncio.run(instant_entry.process_instant_entry(
            msg={"timestamp": _now_iso(), "sender_name": "x"}, tg_id="30500",
            group_id="g1", channel_name="GOLD DIGGERS INSTITUTIONAL",
            text="XAUUSD BUY NOW", direction="BUY", price=None, rs=rs,
            auto_execute=True, bridge=bridge, dpm_candles=None))

    def test_an_instant_entry_is_refused_inside_a_tier_one_window(self, both_dbs):
        db.update_risk_settings({"tg_event_tier_gate_enabled": 1})
        db._rs_cache = None
        db._rs_cache_ts = 0.0
        rs = db.get_risk_settings()

        with mock.patch("backend.src.utils.news_calendar.get_events",
                        return_value=[dict(_fomc(5), ts=__import__("time").time() + 300)]):
            self._run(rs)

        rows = repo.rows()
        assert len(rows) == 1
        assert rows[0]["executed"] == 0
        assert "FOMC" in rows[0]["skip_reason"]

    def test_with_the_gate_off_the_same_signal_gets_past_it(self, both_dbs):
        """The negative control. Without this, a test that "blocks" proves
        only that something else on the path refused, and the gate could be
        doing nothing at all."""
        rs = db.get_risk_settings()
        assert not rs.get("tg_event_tier_gate_enabled")

        # A bridge that answers "no price". It is the next thing the path
        # asks after the gate under test, so reaching it is the proof that
        # the gate stood down -- and it cannot place anything.
        class _NoPriceBridge:
            async def get_tick(self):
                return None

        with mock.patch("backend.src.utils.news_calendar.get_events",
                        return_value=[dict(_fomc(5), ts=__import__("time").time() + 300)]):
            self._run(rs, bridge=_NoPriceBridge())

        rows = repo.rows()
        assert len(rows) == 1
        assert rows[0]["skip_reason"] == "no live price"


class TestTheSwitchCanActuallyBeTurnedOn:
    """A toggle that renders and does not persist is the failure this whole
    page already has a row-count test for. The Parsing tab writes through
    `telegram_controller.update_risk_settings`, so that is what is exercised
    -- not `db.update_risk_settings`, which would prove only that the column
    exists."""

    def test_the_parsing_page_write_reaches_the_column(self, fresh_db):
        from backend.src.controllers import telegram_controller as tg_ctl

        tg_ctl.update_risk_settings({"tg_event_tier_gate_enabled": 1})
        db._rs_cache = None
        db._rs_cache_ts = 0.0

        assert db.get_risk_settings()["tg_event_tier_gate_enabled"] == 1

    def test_and_turning_it_off_again_reaches_it_too(self, fresh_db):
        from backend.src.controllers import telegram_controller as tg_ctl

        tg_ctl.update_risk_settings({"tg_event_tier_gate_enabled": 1})
        tg_ctl.update_risk_settings({"tg_event_tier_gate_enabled": 0})
        db._rs_cache = None
        db._rs_cache_ts = 0.0

        assert not db.get_risk_settings()["tg_event_tier_gate_enabled"]

    def test_it_is_off_on_a_fresh_install(self, fresh_db):
        assert not db.get_risk_settings().get("tg_event_tier_gate_enabled")
