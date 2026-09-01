"""When trading stops, the operator must be told WHY, not just until when.

Found in the 2026-09-01 demo session, on a live halt. The drawdown guard
stopped the account and wrote a precise reason:

    Total drawdown 31.6% from peak $2,140.52 (limit 10%)

What the owner actually saw, on the order he then tried to place, was:

    Trading paused until 17:05 — MT5 order blocked.

The time, and not the cause. `risk_halt_reason` was written in three places in
governor.py, read in exactly one (`scan_staleness.py`), and shown to the user
nowhere at all. A drawdown halt, a daily-loss halt and a give-back halt are
indistinguishable from that message, and they call for completely different
responses -- so the owner could not tell which guard had stopped his account
without someone reading SQLite for him.

No offline test caught it because the offline tests assert the reason is
WRITTEN, and it is. Whether a human ever sees it is not something a fake can
observe.

Nothing here places an order: the refusal happens before any broker call.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.risk import governor

from tests.core.test_manual_market_order_characterization import _FakeBridge

REASON = "Total drawdown 31.6% from peak $2,140.52 (limit 10%)"


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge(order_result={"ticket": 4242, "fill_price": 2400.0})
    e._cfg = {}
    return e


def _halt(reason=REASON, mins=10):
    with db.db():
        db.set_app_config("trade_pause_until", str(time.time() + mins * 60))
        db.set_app_config("risk_halt_reason", reason)


def _place(engine):
    return asyncio.run(TradingRuntime.open_manual_market_order(
        engine, "BUY", stop_loss=2390.0))


class TestTheRefusalSaysWhy:
    def test_the_reason_is_in_the_message(self, fresh_db, engine):
        _halt()

        with pytest.raises(ValueError) as exc:
            _place(engine)

        assert REASON in str(exc.value)

    def test_the_specific_numbers_survive(self, fresh_db, engine):
        """Not a category label. "Drawdown limit reached" would pass a looser
        assertion and still leave the owner unable to see how far past the
        limit he is, which is what decides what he does next."""
        _halt()

        with pytest.raises(ValueError) as exc:
            _place(engine)

        text = str(exc.value)
        assert "31.6%" in text and "$2,140.52" in text and "10%" in text

    def test_the_resume_time_is_still_there(self, fresh_db, engine):
        """Adding the cause must not cost the time. Both matter: one says what
        to fix, the other says whether waiting is an option."""
        _halt()

        with pytest.raises(ValueError) as exc:
            _place(engine)

        assert "paused until" in str(exc.value).lower()

    def test_it_still_refuses_when_no_reason_was_stored(self, fresh_db, engine):
        """The halt is what blocks the order. A missing reason may not turn a
        refusal into a placement -- that would make an unexplained halt the
        one that lets orders through."""
        with db.db():
            db.set_app_config("trade_pause_until", str(time.time() + 600))

        with pytest.raises(ValueError):
            _place(engine)

        assert engine._bridge.place_order_calls == []

    def test_a_stale_reason_from_an_older_halt_is_not_shown(self, fresh_db, engine):
        """`risk_halt_reason` is not cleared on resume, so a reason from last
        week's halt would be attached to today's. Only report it while it
        belongs to the halt in force."""
        with db.db():
            db.set_app_config("risk_halt_reason", "Daily loss limit hit: last Tuesday")
            db.set_app_config("trade_pause_until", "0")

        # Not halted at all: the order goes through, reason or no reason.
        assert _place(engine)["mt5_ticket"] == 4242

    def test_no_order_reaches_the_broker(self, fresh_db, engine):
        _halt()

        with pytest.raises(ValueError):
            _place(engine)

        assert engine._bridge.place_order_calls == []


class TestTheGovernorExposesIt:
    def test_the_reason_can_be_read_back(self, fresh_db):
        _halt()

        assert governor.halt_reason() == REASON

    def test_it_is_empty_when_not_halted(self, fresh_db):
        """Not the last halt's text. Nothing is paused, so there is no reason
        to show, and showing a stale one would be worse than showing none."""
        with db.db():
            db.set_app_config("risk_halt_reason", REASON)
            db.set_app_config("trade_pause_until", "0")

        assert governor.halt_reason() == ""

    def test_it_is_empty_when_halted_with_nothing_recorded(self, fresh_db):
        with db.db():
            db.set_app_config("trade_pause_until", str(time.time() + 600))

        assert governor.halt_reason() == ""

    def test_a_database_failure_returns_empty_rather_than_raising(
        self, fresh_db, monkeypatch,
    ):
        """This is read to render a badge on every header refresh. A failure
        must cost the explanation, not the page.

        The halt is set up FIRST, and only the reason read is broken. An
        earlier version of this test patched `db_module.get_app_config` with
        nothing paused: `is_trading_paused` reads through `app_config_repo`
        instead, so it returned False, `halt_reason` took its early return, and
        the patched call was never reached. It passed with the except clause
        mutated to `raise` -- vacuous, and only mutation testing showed it.
        """
        _halt()
        assert governor.is_trading_paused() is True, "the halt did not take"

        def _boom(*a, **k):
            raise RuntimeError("no database")
        monkeypatch.setattr(governor.db_module, "get_app_config", _boom)

        assert governor.halt_reason() == ""


class TestTheUiShowsIt:
    def test_the_header_badge_reads_the_reason(self):
        from tests.frontend._source import module_source

        src = module_source("frontend/app.py")

        assert "halt_reason" in src, (
            "the paused badge still says only when trading resumes"
        )

    def test_the_pause_dialog_reads_the_reason(self):
        from tests.frontend._source import module_source

        src = module_source("frontend/app.py")
        dialog = src[src.index("_on_pause_dialog_change"):]

        assert "halt_reason" in dialog

    def test_the_ui_goes_through_a_controller(self):
        """The frontend does not reach into services.risk for this."""
        from tests.frontend._source import module_source

        src = module_source("frontend/app.py")

        assert "services.risk" not in src
