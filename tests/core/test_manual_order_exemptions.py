"""What the Market Order button is, and is not, exempt from.

Written after getting this wrong out loud during the 2026-09-01 demo session.
Reading only `manual_market_order.py` and finding no gate in it, I told the
owner the manual path bypassed everything including the risk halt, and he
confirmed that was intended. It was a description of a system that does not
exist: `manual_market_order` calls `open_trade`, and the gates live there.

The real split, and it is a coherent one:

  EXEMPT -- the **scheduling** gates, which exist to govern when the app may
  trade by itself:
    * the trading schedule (already pinned at test_trading_schedule.py:203)
    * the news blackout

  ENFORCED -- the **protective** limits, which exist to bound loss and
  exposure however the order was asked for:
    * `is_trading_paused` -- the risk halt, including the daily-loss halt
    * `max_open_trades`

That is worth pinning in both directions. A future reader finding no
`is_trading_paused()` in `manual_market_order.py` may reach for the same wrong
conclusion I did and "fix" it by adding one, giving the button two checks; or
may read "manual orders are always exempt" in the schedule docs and widen it to
the halt.

The enforced half also bounds stage3/050's demo 5 in the owner's favour: the
daily-loss halt stops the Market Order button too, not only automated signals.

No real or demo order is placed anywhere: the bridge is a fake and its call log
is what the assertions read.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.risk import governor

from tests.core.test_manual_market_order_characterization import _FakeBridge


@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _FakeBridge(order_result={"ticket": 4242, "fill_price": 2400.0})
    e._cfg = {}
    return e


def _place(engine):
    return asyncio.run(TradingRuntime.open_manual_market_order(
        engine, "BUY", stop_loss=2390.0))


def _halt_trading(reason="Daily loss limit hit: test"):
    with db.db():
        db.set_app_config("trade_pause_until", str(time.time() + 3600))
        db.set_app_config("risk_halt_reason", reason)


class TestTheProtectiveLimitsAreEnforced:
    def test_the_halt_is_actually_in_force(self, fresh_db):
        """Control first. Without it, a manual order refused for some other
        reason would look like the halt working."""
        assert governor.is_trading_paused() is False
        _halt_trading()

        assert governor.is_trading_paused() is True

    def test_a_manual_order_is_refused_while_trading_is_halted(self, fresh_db,
                                                               engine):
        _halt_trading()

        with pytest.raises(ValueError, match="paused"):
            _place(engine)

        assert engine._bridge.place_order_calls == [], (
            "an order reached the broker while trading was halted"
        )

    def test_this_is_what_demo_5_covers(self, fresh_db, engine):
        """Named so the connection survives. stage3/050's demo halts on the
        daily-loss limit and sends another signal -- the automated path. This
        shows the manual path is stopped by the same halt, so demo 5's result
        extends to the Market Order button rather than saying nothing about
        it."""
        _halt_trading("Daily loss limit hit: $-50.00 today vs -$43.56")

        with pytest.raises(ValueError):
            _place(engine)

    def test_a_manual_order_is_refused_at_max_open_trades(self, fresh_db,
                                                          engine, monkeypatch):
        """The cap is counted in open_trade, against the table about to
        receive the INSERT. Counting is faked rather than seeding rows: the
        subject here is the gate, not the query, and a hand-built row is one
        schema change away from testing nothing."""
        from backend.src.services.trading import trade_repo
        monkeypatch.setattr(trade_repo, "count_open_trades", lambda: 5)
        with db.db() as conn:
            conn.execute("UPDATE vantage_risk_settings SET max_open_trades=5 WHERE id=1")

        with pytest.raises(ValueError, match="Max open trades"):
            _place(engine)

        assert engine._bridge.place_order_calls == []

    def test_one_slot_free_still_places(self, fresh_db, engine, monkeypatch):
        """Negative control for the test above: at 4 of 5 it must go through,
        or the refusal proves only that something refused."""
        from backend.src.services.trading import trade_repo
        monkeypatch.setattr(trade_repo, "count_open_trades", lambda: 4)
        with db.db() as conn:
            conn.execute("UPDATE vantage_risk_settings SET max_open_trades=5 WHERE id=1")

        assert _place(engine)["mt5_ticket"] == 4242


class TestTheSchedulingGatesAreNot:
    def test_a_manual_order_places_during_a_news_blackout(self, fresh_db,
                                                          engine, monkeypatch):
        monkeypatch.setattr(
            "backend.src.utils.news_calendar.check_news_blackout",
            lambda *a, **k: (False, "News blackout — ISM Manufacturing PMI (USD)"),
        )

        assert _place(engine)["mt5_ticket"] == 4242

    def test_neither_module_on_the_path_consults_the_scheduling_gates(self):
        """The patch above only shows the order placed. This shows why: no
        module on the manual path asks. Checked on both files, because the
        gate that caught me out was in the one I had not read."""
        import pathlib

        from backend.src.services.trading import manual_market_order as mmo
        from backend.src.services.trading import open_trade as ot

        for mod in (mmo, ot):
            src = pathlib.Path(mod.__file__).read_text(encoding="utf-8")
            assert "check_news_blackout" not in src, mod.__name__
            assert "check_trading_schedule" not in src, mod.__name__


class TestWhereTheGatesActuallyLive:
    def test_the_manual_module_itself_holds_none_of_them(self):
        """The fact that misled me, recorded as a fact rather than a
        conclusion: manual_market_order.py really does contain no gate. It
        delegates to open_trade, which is the shared order-placement path, and
        that is where the protective limits sit -- so every caller gets them
        and no caller can forget."""
        import pathlib

        from backend.src.services.trading import manual_market_order as mmo

        src = pathlib.Path(mmo.__file__).read_text(encoding="utf-8")

        assert "is_trading_paused" not in src
        assert "max_open_trades" not in src

    def test_and_the_shared_path_holds_both(self):
        import pathlib

        from backend.src.services.trading import open_trade as ot

        src = pathlib.Path(ot.__file__).read_text(encoding="utf-8")

        assert "is_trading_paused" in src
        assert "max_open_trades" in src
