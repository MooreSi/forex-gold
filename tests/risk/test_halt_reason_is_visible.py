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
import pathlib
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
    """Retargeted 2026-09-18 from the NiceGUI shell to the React dashboard.

    The bug this class exists for is unchanged and is not a NiceGUI bug: the
    halt reason was written in three places, read in one, and shown to the
    owner nowhere. The question "does a human ever see it?" has to be asked of
    whatever the UI is at the time.

    So the assertions moved rather than being deleted. Two halves:

    * the reason must leave the backend — an HTTP field the dashboard can read;
    * the dashboard must render that field rather than fetch it and drop it.

    The second half reads the TypeScript source, which is exactly what the
    NiceGUI version did with Python source and carries the same limitation:
    it proves the field is referenced, not that it is visible. The component
    test `TradingPanel`/`AppHeader` covers the rendering; this covers the wiring
    end to end.
    """

    API_ROOT = pathlib.Path(__file__).resolve().parents[2] / "backend" / "src" / "api"
    WEB_ROOT = pathlib.Path(__file__).resolve().parents[2] / "frontend" / "src"

    def test_the_backend_offers_the_reason_over_http(self):
        """Both surfaces that show it, because they are read in different
        places: the header is on every tab, and the Trading tab disables its
        controls with it."""
        from backend.src.api.routers import system as system_router
        from backend.src.api.routers import trading as trading_router

        header = (self.API_ROOT / "routers" / "system.py").read_text(encoding="utf-8")
        halt = (self.API_ROOT / "routers" / "trading.py").read_text(encoding="utf-8")

        # The header carries BOTH halts as of 2026-09-18 -- the risk governor's
        # and the circuit breaker's, which are stored under different keys and
        # of which only the governor's used to reach this payload. The claim
        # this test makes is unchanged: the REASON has to get to the screen.
        assert "trading_pause_status" in header, (
            "the header payload no longer carries why trading stopped")
        assert "trading_pause_status" in halt, (
            "the halt endpoint no longer asks for it")
        assert system_router.router.prefix == "/api/system"
        assert trading_router.router.prefix == "/api/trading"

    def test_the_header_renders_the_reason(self):
        src = (self.WEB_ROOT / "components" / "shell" / "AppHeader.tsx").read_text(
            encoding="utf-8")

        assert "pause.reason" in src, (
            "the header badge no longer shows why trading stopped"
        )
        assert "pause.until" in src, (
            "and it no longer says when trading resumes -- the 2026-09-01 "
            "session got the time and not the cause; both are wanted"
        )

    def test_the_trading_controls_say_why_they_are_disabled(self):
        src = (self.WEB_ROOT / "components" / "trading" / "hooks"
               / "useTradingController.ts").read_text(encoding="utf-8")

        assert "h.reason" in src, (
            "the Trading tab greys its controls without reading the halt reason "
            "-- a disabled Execute button with no explanation is "
            "indistinguishable from a broken one"
        )

    def test_the_ui_goes_through_a_controller(self):
        """The API layer does not reach into services.risk for this."""
        for name in ("system.py", "trading.py"):
            src = (self.API_ROOT / "routers" / name).read_text(encoding="utf-8")
            assert "services.risk" not in src, name

    def test_these_checks_can_fail(self):
        """Negative control. Every assertion above is a substring search over a
        file, which is the kind of check that quietly passes against the wrong
        file or an empty one."""
        assert (self.API_ROOT / "routers" / "system.py").stat().st_size > 0
        assert (self.WEB_ROOT / "components" / "shell" / "AppHeader.tsx").stat().st_size > 0
        assert "halt_reason" not in (self.WEB_ROOT / "lib" / "cn.ts").read_text(
            encoding="utf-8")
