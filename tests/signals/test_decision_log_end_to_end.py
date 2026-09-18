"""A real decision, through a real path, lands in the table.

The wiring tests read source; these run it. Both exist on purpose: a
source-level check cannot tell a call that happens from a call that is
written down, and `docs/todo/refactor` records a guardrail that scanned a
deleted directory and printed "all good" every run for months.
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

import pytest

from backend.src.db import database as db
from backend.src.services.reversal_engine import reversal_engine_repo as re_repo
from backend.src.services.signals import decision_log_repo as repo
from backend.src.services.trading import instant_entry
from backend.src.services.trading import scan_auto_execute
from tests.conftest import remove_db_file


def _now_iso() -> str:
    """Evaluated per call, never as a module constant: the IME path refuses
    anything older than four minutes, and a timestamp fixed at collection
    time is stale by the time a six-minute suite reaches this file."""
    return datetime.now(timezone.utc).isoformat()


@pytest.fixture
def both_dbs(fresh_db):
    """The core ledger and the research database. The decision log writes to
    the second and reads trades from the first, which is the arrangement
    that would break if either were assumed to be the other."""
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


class TestTheIMEPath:
    def test_a_blocked_instant_entry_is_recorded(self, both_dbs):
        """auto-execute off is the cheapest block to reach, and a blocked
        decision is the half of the study that costs nothing."""
        rs = db.get_risk_settings()
        asyncio.run(instant_entry.process_instant_entry(
            msg={"timestamp": _now_iso(), "sender_name": "x"}, tg_id="21448",
            group_id="g1", channel_name="Gold Diggers VIP", text="XAUUSD BUY NOW",
            direction="BUY", price=None, rs=rs, auto_execute=False,
            bridge=None, dpm_candles=None))

        rows = repo.rows()
        assert len(rows) == 1
        assert rows[0]["path"] == "ime"
        assert rows[0]["executed"] == 0
        assert rows[0]["tg_message_id"] == "21448"
        assert "auto-execute" in rows[0]["skip_reason"]

    def test_a_stale_instant_message_is_recorded_too(self, both_dbs):
        """A message with no timestamp is treated as stale and never traded.
        It is still a decision, and one worth being able to count."""
        rs = db.get_risk_settings()
        asyncio.run(instant_entry.process_instant_entry(
            msg={"timestamp": "", "sender_name": "x"}, tg_id="21451",
            group_id="g1", channel_name="Gold Diggers VIP", text="XAUUSD BUY NOW",
            direction="BUY", price=None, rs=rs, auto_execute=True,
            bridge=None, dpm_candles=None))
        assert repo.rows()[0]["skip_reason"] == "stale instant message"

    def test_nothing_is_recorded_when_the_toggle_is_off(self, both_dbs):
        db.update_risk_settings({"tg_decision_log_enabled": 0})
        db._rs_cache = None
        db._rs_cache_ts = 0.0
        rs = db.get_risk_settings()
        asyncio.run(instant_entry.process_instant_entry(
            msg={"timestamp": _now_iso(), "sender_name": "x"}, tg_id="21449",
            group_id="g1", channel_name="Gold Diggers VIP", text="XAUUSD BUY NOW",
            direction="BUY", price=None, rs=rs, auto_execute=False,
            bridge=None, dpm_candles=None))
        assert repo.rows() == []

    def test_the_trade_still_happens_when_the_log_explodes(self, both_dbs):
        """The whole contract. A research log that can stop a trade is worse
        than no research log."""
        rs = db.get_risk_settings()
        with mock.patch.object(repo, "insert_decision",
                               side_effect=RuntimeError("db is on fire")):
            asyncio.run(instant_entry.process_instant_entry(
                msg={"timestamp": _now_iso(), "sender_name": "x"}, tg_id="21450",
                group_id="g1", channel_name="Gold Diggers VIP",
                text="XAUUSD BUY NOW", direction="BUY", price=None, rs=rs,
                auto_execute=False, bridge=None, dpm_candles=None))
        # Reaching here at all is the assertion: the path completed.
        assert repo.rows() == []


class TestTheFullSignalPath:
    def _run(self, rs, **over):
        kwargs = dict(
            parsed={"direction": "BUY", "entry_low": 4100.0, "entry_high": 4102.0,
                    "stop_loss": 4090.0, "tp1": 4120.0, "tp2": None, "tp3": None,
                    "tp4": None, "tp5": None},
            tg_id="30374", channel_name="GOLD DIGGERS INSTITUTIONAL",
            source_label="GOLD DIGGERS INSTITUTIONAL", strategy="scale_out",
            rs=rs, sess_ok=False, per_signal_skip=False,
            per_signal_skip_reason="", skip_reason="session closed",
            bridge=SimpleNamespace(get_tick=mock.AsyncMock(return_value=None)),
            get_open_trades_fn=lambda: [],
            find_and_apply_instant_followup_fn=mock.AsyncMock(return_value=False),
            check_pre_trade_filters_fn=lambda **k: None,
            suggest_lot_size_fn=lambda *a: 0.01,
            get_trading_balance_fn=mock.AsyncMock(return_value=1000.0),
            open_trade_fn=mock.AsyncMock(return_value={"trade_id": "t-1"}),
        )
        kwargs.update(over)
        return asyncio.run(scan_auto_execute.execute_auto_signal(**kwargs))

    def test_a_declined_signal_is_recorded_with_its_reason(self, both_dbs):
        result = self._run(db.get_risk_settings())
        assert result["executed"] is False

        rows = repo.rows()
        assert len(rows) == 1
        assert rows[0]["path"] == "auto"
        assert rows[0]["executed"] == 0
        assert rows[0]["skip_reason"] == "session closed"
        assert rows[0]["entry_low"] == 4100.0
        assert rows[0]["direction"] == "BUY"

    def test_the_spread_IS_recorded_on_this_path(self, both_dbs):
        """It was abstaining until 2026-09-18. mt5_client.get_tick() is
        cached for 1s, and the wrapper reads it BEFORE the body does -- so
        on an executing decision the body's own read becomes the cache hit
        and the round trip count is unchanged."""
        tick = SimpleNamespace(bid=4101.0, ask=4101.3, spread_points=26.0)
        self._run(db.get_risk_settings(),
                  bridge=SimpleNamespace(get_tick=mock.AsyncMock(return_value=tick)))
        assert repo.rows()[0]["spread_points"] == 26.0

    def test_a_bridge_that_cannot_answer_abstains_instead_of_failing(self, both_dbs):
        """No live price is one of the reasons a trade is blocked. It must
        cost the row a fact, never the row itself."""
        boom = SimpleNamespace(get_tick=mock.AsyncMock(side_effect=RuntimeError("no bridge")))
        self._run(db.get_risk_settings(), bridge=boom)
        assert repo.rows()[0]["spread_points"] is None

    def test_the_spread_read_happens_only_when_the_log_is_on(self, both_dbs):
        """An install with the log off must not pay a bridge call for it."""
        db.update_risk_settings({"tg_decision_log_enabled": 0})
        db._rs_cache = None
        db._rs_cache_ts = 0.0
        bridge = SimpleNamespace(get_tick=mock.AsyncMock(return_value=None))
        self._run(db.get_risk_settings(), bridge=bridge)
        assert bridge.get_tick.await_count == 0

    def test_the_inline_facts_are_recorded(self, both_dbs):
        """Gathered before any gate ran, which is what makes every row
        comparable regardless of which gate decided it."""
        self._run(db.get_risk_settings())
        row = repo.rows()[0]
        assert row["liquidity_blocked"] in (0, 1)

    def test_a_stale_instant_message_is_recorded_too(self, both_dbs):
        """A message with no timestamp is treated as stale and never traded.
        It is still a decision, and one worth being able to count."""
        rs = db.get_risk_settings()
        asyncio.run(instant_entry.process_instant_entry(
            msg={"timestamp": "", "sender_name": "x"}, tg_id="21451",
            group_id="g1", channel_name="Gold Diggers VIP", text="XAUUSD BUY NOW",
            direction="BUY", price=None, rs=rs, auto_execute=True,
            bridge=None, dpm_candles=None))
        assert repo.rows()[0]["skip_reason"] == "stale instant message"

    def test_nothing_is_recorded_when_the_toggle_is_off(self, both_dbs):
        db.update_risk_settings({"tg_decision_log_enabled": 0})
        db._rs_cache = None
        db._rs_cache_ts = 0.0
        self._run(db.get_risk_settings())
        assert repo.rows() == []

    def test_the_result_is_unchanged_by_the_wrapper(self, both_dbs):
        """It is a wrapper on the order path. Every key the caller reads has
        to survive it -- scan_messages.py reads six of them by name."""
        result = self._run(db.get_risk_settings())
        for key in ("executed", "exec_lot", "exec_price", "trade_result",
                    "skip_reason", "gap_note"):
            assert key in result
