"""Never add to a loser; count open risk toward the daily loss limit.

Owner, 2026-10-22, after three Reversal Engine BUYs lost $59.68 between 00:10
and 01:00 UTC (docs/todo/026):

    "implement Never add to a loser under risk settings which applies to all
     channels"
    "Count open risk toward the daily limit - add risk toggle for this"

Two switches on Settings > Risk, both OFF by default:

  * `never_add_to_loser_enabled` -- refuse an order while an open trade in the
    same direction, from any source, is losing at the current price;
  * `daily_limit_counts_open_risk` -- refuse an order when today's closed P&L,
    minus what every open trade loses at its stop, minus what the new trade
    risks, would pass the daily loss limit.

Both are checked in `open_trade`, the funnel every market order passes, so a
refusal never reaches `place_order`.

Nothing here reaches a broker. The bridge is a fake: `get_account` returns a
canned balance, and `place_order` records the call and returns a canned fill.
"""
from __future__ import annotations

import asyncio
import inspect
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.runtime import TradingRuntime
from backend.src.services.risk import governor
from backend.src.services.risk import position_guards as pg
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading import open_trade as open_trade_mod
from tests._fakes import _FakeBridge

BALANCE = 948.51          # the VPS's opening balance on 2026-10-22
LOT = 0.04                # $20 at a 5-point stop


@pytest.fixture(autouse=True)
def no_telegram(monkeypatch):
    async def _send(*a, **k):
        return None
    monkeypatch.setattr(telegram_alerts, "send_message", _send)


class _Bridge(_FakeBridge):
    """A fake: a canned account balance and a recording `place_order`."""

    def __init__(self, tick, balance=BALANCE):
        super().__init__(tick=tick)
        self.balance = balance
        self.account_reads = 0
        self.place_order_calls = []

    async def get_fresh_tick(self):
        return self._tick

    async def get_account(self):
        self.account_reads += 1
        return {"balance": self.balance}

    async def place_order(self, direction, lots, sl, tp, comment=""):
        self.place_order_calls.append((direction, lots, sl, tp))
        return {"ticket": 999, "fill_price": self._tick.ask}

    async def get_candles(self, timeframe="M5", count=200):
        return []


def _tick(bid, ask=None):
    return SimpleNamespace(bid=bid, ask=ask if ask is not None else bid + 0.21,
                           spread_points=21.0)


def _settings(**kw):
    sets = ", ".join(f"{k}=?" for k in kw)
    with db.db() as conn:
        conn.execute(f"UPDATE vantage_risk_settings SET {sets} WHERE id=1", tuple(kw.values()))
    db._rs_cache = None
    db._rs_cache_ts = 0.0


def _row(trade_id, direction, entry, sl, lots=LOT, status="open", net=0.0,
         close_time=None, signal_id=None, initial_risk=None, initial_sl=None):
    signal_id = signal_id or f"sig-{trade_id}"
    if close_time is not None:          # never in yesterday, just after the rollover
        close_time = max(close_time, governor.calendar_day_start_ts() + 1)
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Reversal Engine", direction, entry or 0, entry or 0, sl, lots,
             "active", time.time()))
        conn.execute(
            "INSERT INTO vantage_simulated_trades (trade_id,signal_id,mt5_ticket,direction,"
            "entry_low,entry_high,entry_price,lot_size,remaining_lots,stop_loss,status,"
            "open_time,close_time,net_pnl,strategy,initial_sl,initial_risk) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (trade_id, signal_id, 1000 + len(trade_id), direction, entry or 0, entry or 0,
             entry, lots, lots, sl, status, time.time() - 600, close_time, net, "scalp",
             initial_sl if initial_sl is not None else sl, initial_risk))


def _check(bridge, direction="BUY", stop=None, lots=LOT, signal_id="new",
           strategy="scalp"):
    tick = bridge._tick
    if stop is None:
        stop = (tick.ask - 5.0) if direction == "BUY" else (tick.bid + 5.0)
    rs = db.get_risk_settings()
    return asyncio.run(pg.check(bridge, signal_id=signal_id, direction=direction,
                                stop_loss=stop, lot_size=lots, strategy=strategy,
                                tick=tick, rs=rs))


# ── both are off until somebody turns them on ────────────────────────────────

class TestOffByDefault:
    def test_both_switches_are_columns_that_default_to_off(self, fresh_db):
        rs = db.get_risk_settings()
        assert rs["never_add_to_loser_enabled"] == 0
        assert rs["daily_limit_counts_open_risk"] == 0

    def test_off_a_losing_trade_does_not_refuse_and_nothing_is_read(self, fresh_db):
        _settings(risk_governor_enabled=1, max_daily_loss_pct=4.0)
        _row("t1", "BUY", 4120.99, 4115.99)
        _row("c1", "BUY", 4125.0, 4120.0, status="closed", net=-20.0,
             close_time=time.time() - 60)
        bridge = _Bridge(_tick(4117.80))
        _check(bridge)                                  # does not raise
        assert bridge.account_reads == 0


# ── A. never add to a loser ──────────────────────────────────────────────────

class TestNeverAddToALoser:
    @pytest.fixture(autouse=True)
    def _on(self, fresh_db):
        _settings(never_add_to_loser_enabled=1)

    def test_a_buy_is_refused_while_an_open_buy_is_losing(self):
        _row("t1", "BUY", 4120.99, 4115.99)
        with pytest.raises(ValueError, match="losing"):
            _check(_Bridge(_tick(4117.67, 4117.88)))

    def test_a_sell_is_refused_while_an_open_sell_is_losing(self):
        _row("t1", "SELL", 4100.00, 4105.00)
        with pytest.raises(ValueError, match="losing"):
            _check(_Bridge(_tick(4102.00)), direction="SELL")

    def test_a_buy_is_allowed_while_the_open_buy_is_in_profit(self):
        _row("t1", "BUY", 4110.00, 4105.00)
        _check(_Bridge(_tick(4117.67, 4117.88)))

    def test_a_trade_that_just_opened_is_not_a_loser_because_of_the_spread(self):
        """Bought at the ask a moment ago: the bid is below entry by the
        spread, but the ask is not. Comparing ask with entry keeps the spread
        out of it."""
        _row("t1", "BUY", 4118.20, 4113.20)
        _check(_Bridge(_tick(4117.99, 4118.20)))

    def test_a_losing_trade_in_the_other_direction_does_not_refuse(self):
        _row("t1", "SELL", 4100.00, 4105.00)
        _check(_Bridge(_tick(4117.67, 4117.88)))

    def test_it_applies_whatever_source_opened_the_losing_trade(self):
        _row("t1", "BUY", 4120.99, 4115.99)
        with db.db() as conn:
            conn.execute("UPDATE vantage_simulated_trades SET tg_source=? "
                         "WHERE trade_id='t1'", ("GOLD DIGGERS INSTITUTIONAL",))
        with pytest.raises(ValueError, match="losing"):
            _check(_Bridge(_tick(4117.67, 4117.88)))

    def test_the_legs_of_the_signal_being_opened_are_not_counted(self):
        _row("t1", "BUY", 4120.99, 4115.99, signal_id="same")
        _check(_Bridge(_tick(4117.67, 4117.88)), signal_id="same")

    def test_a_row_with_no_fill_price_yet_is_not_counted(self):
        """A grid placeholder's entry_price is 0 until its fill is matched."""
        _row("t1", "BUY", 0.0, 4115.99)
        _check(_Bridge(_tick(4117.67, 4117.88)))

    def test_a_closed_losing_trade_is_not_counted(self):
        _row("t1", "BUY", 4125.0, 4120.0, status="closed", net=-20.0,
             close_time=time.time() - 60)
        _check(_Bridge(_tick(4117.67, 4117.88)))


# ── B. open risk counts toward the daily loss limit ──────────────────────────

class TestOpenRiskCountsTowardTheDailyLimit:
    @pytest.fixture(autouse=True)
    def _on(self, fresh_db):
        # The Risk Governor stays OFF: the daily loss halt does not need it.
        _settings(daily_limit_counts_open_risk=1, risk_governor_enabled=0,
                  max_daily_loss_pct=4.0)

    def test_a_closed_loss_plus_the_new_trades_risk_past_the_limit_is_refused(self):
        """22 Oct, 00:15: -$20 closed, $20 more at risk, limit $37.94."""
        _row("c1", "BUY", 4125.0, 4120.0, status="closed", net=-20.0,
             close_time=time.time() - 60)
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4120.78, 4120.99), balance=BALANCE - 20.0))

    def test_the_new_trade_alone_within_the_limit_is_allowed(self):
        _check(_Bridge(_tick(4120.78, 4120.99)))

    def test_an_open_trades_risk_at_its_stop_counts(self):
        _row("t1", "BUY", 4120.99, 4115.99)             # $20 at its stop
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4122.00)))

    def test_a_stop_already_past_entry_risks_nothing(self):
        _row("t1", "BUY", 4110.00, 4112.00)             # stop locked in profit
        _check(_Bridge(_tick(4122.00)))

    def test_an_unmoved_stops_recorded_initial_risk_is_used_when_larger(self):
        """A grid row's lot_size is one leg; initial_risk covers every leg."""
        _row("t1", "BUY", 4120.99, 4115.99, initial_risk=60.0)
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4122.00)), lots=0.01)

    def test_a_grid_templates_new_risk_counts_every_leg(self, monkeypatch):
        from backend.src.services.broker import ea_templates
        monkeypatch.setattr(ea_templates, "get_ea_template",
                            lambda name: {"mode": "grid", "anchors": 1, "pendings": 2})
        # $15 a leg x 3 legs = $45, past $37.94; one leg alone would pass.
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4122.00)), lots=0.03, strategy="template:GD VIP - Grid")

    def test_it_works_with_the_risk_governor_off_as_the_daily_loss_halt_does(self):
        """governor.apply_daily_loss_halt_on_close runs whatever
        risk_governor_enabled says, so the limit this counts against does too."""
        assert db.get_risk_settings()["risk_governor_enabled"] == 0
        _row("t1", "BUY", 4120.99, 4115.99)
        _row("t2", "BUY", 4125.99, 4120.99)
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4126.00)))

    def test_no_daily_loss_limit_means_nothing_to_count_against(self):
        _settings(max_daily_loss_pct=0)
        _row("t1", "BUY", 4120.99, 4115.99)
        _row("t2", "BUY", 4125.99, 4120.99)
        _check(_Bridge(_tick(4126.00)))

    def test_a_loss_before_a_manual_resume_is_forgiven_as_the_halt_forgives_it(self):
        """check_daily_loss_limit counts from the last Resume, so resuming
        after it fires is not undone by the next order either."""
        _row("c1", "BUY", 4125.0, 4120.0, status="closed", net=-30.0,
             close_time=time.time() - 60)
        with db.db():
            db.set_app_config("daily_loss_baseline_ts", str(time.time()))
        _check(_Bridge(_tick(4122.00), balance=BALANCE - 30.0))
        with db.db():
            db.set_app_config("daily_loss_baseline_ts", "0")
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4122.00), balance=BALANCE - 30.0))

    def test_no_balance_is_not_evaluated(self):
        """The bridge could not say. The existing daily-loss halt still
        applies; this check does not guess a balance."""
        _row("t1", "BUY", 4120.99, 4115.99)
        _check(_Bridge(_tick(4122.00), balance=0))

    def test_the_limit_is_the_one_the_daily_loss_halt_uses(self):
        """Same day, same closed-P&L sum, same percentage and balance as
        governor.check_daily_loss_limit, the halt that fired on 2026-10-22."""
        _row("c1", "BUY", 4125.0, 4120.0, status="closed", net=-30.0,
             close_time=time.time() - 60)
        rs = db.get_risk_settings()
        # The halt does not trip at -$30 of a 4% limit on $948.51 ...
        assert governor.check_daily_loss_limit(rs, BALANCE - 30.0) is None
        # ... and $7.90 more at risk is still inside it ($37.90 < $37.94) ...
        _check(_Bridge(_tick(4122.00), balance=BALANCE - 30.0), lots=0.0158)
        # ... while $8.00 more is not.
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4122.00), balance=BALANCE - 30.0), lots=0.016)


# ── wired into the funnel ────────────────────────────────────────────────────

@pytest.fixture
def engine(fresh_db):
    e = TradingRuntime.__new__(TradingRuntime)
    e._bridge = _Bridge(_tick(4117.67, 4117.88))
    e._cfg = {}
    _settings(max_open_trades=5)    # room for the open rows these tests add
    return e


def _market(engine, **kw):
    kw.setdefault("stop_loss", 4112.88)
    kw.setdefault("lot_size", LOT)
    kw.setdefault("take_profit", 4130.0)
    return asyncio.run(TradingRuntime.open_manual_market_order(engine, "BUY", **kw))


class TestWiredIntoOpenTrade:
    def test_a_refused_order_never_reaches_place_order(self, engine):
        _settings(never_add_to_loser_enabled=1)
        _row("t1", "BUY", 4120.99, 4115.99)
        with pytest.raises(ValueError, match="losing"):
            _market(engine, source_name="Set & Forget Auto")
        assert engine._bridge.place_order_calls == []

    def test_switched_off_the_same_order_is_placed(self, engine):
        """Negative control for the test above."""
        _row("t1", "BUY", 4120.99, 4115.99)
        _market(engine, source_name="Set & Forget Auto")
        assert len(engine._bridge.place_order_calls) == 1

    def test_the_open_risk_refusal_never_reaches_place_order(self, engine):
        _settings(daily_limit_counts_open_risk=1, max_daily_loss_pct=4.0)
        _row("t1", "SELL", 4100.00, 4120.99)            # $83.96 at its stop
        with pytest.raises(ValueError, match="daily loss limit"):
            _market(engine, source_name="Set & Forget Auto")
        assert engine._bridge.place_order_calls == []

    def test_the_guard_sits_in_open_trade_after_the_price_is_known(self):
        src = inspect.getsource(open_trade_mod.open_trade)
        assert "position_guards.check(" in src
        assert src.index("position_guards.check(") > src.index("No live price available")
        assert src.index("position_guards.check(") < src.index("bridge.place_order(")


# ── the 2026-10-22 sequence ──────────────────────────────────────────────────

class TestTheTwentySecondOfOctober:
    """00:10 BUY at 4125.66 stopped at 00:13; 00:15 BUY at 4120.99 open and
    losing; 00:24 BUY at 4117.88."""

    def _morning(self):
        _row("ff4a28", "BUY", 4125.66, 4120.66, status="closed", net=-20.0,
             close_time=time.time() - 120)

    def test_never_add_to_a_loser_refuses_the_third_buy_not_the_second(self, fresh_db):
        _settings(never_add_to_loser_enabled=1)
        self._morning()
        _check(_Bridge(_tick(4120.78, 4120.99)))        # 00:15: first already closed
        _row("3fc7f9", "BUY", 4120.99, 4115.99)
        with pytest.raises(ValueError, match="losing"):
            _check(_Bridge(_tick(4117.67, 4117.88)))    # 00:24

    def test_counting_open_risk_refuses_the_second_buy(self, fresh_db):
        _settings(daily_limit_counts_open_risk=1, max_daily_loss_pct=4.0)
        self._morning()
        with pytest.raises(ValueError, match="daily loss limit"):
            _check(_Bridge(_tick(4120.78, 4120.99), balance=BALANCE - 20.0))


# ── an order in flight is not known to be safe (2026-10-22, 10:55) ───────────

def _in_flight(signal_id, direction="BUY", resting=False):
    """A signal claimed for opening (`activating`) whose order has not been
    recorded yet -- what open_trade_from_signal leaves while the EA acks."""
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id,source_name,direction,entry_low,"
            "entry_high,stop_loss,lot_size,status,created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (signal_id, "Reversal Engine", direction, 4081.0, 4082.0, 4076.64, 0.03,
             "activating", time.time()))
        if resting:
            conn.execute(
                "INSERT INTO vantage_pending_orders (trade_id,signal_id,channel_name,"
                "direction,price,stop_loss,tps_json,pcts_json,be_at_pos,tp_open,lot_size,"
                "ea_ticket,status,created_at,strategy) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (f"po-{signal_id}", signal_id, "Reversal Engine", direction, 4081.0,
                 4076.64, "{}", "[]", 0, 0, 0.03, 555, "working", time.time(), "limit_runner"))


class TestAnOrderInFlight:
    """Owner, 2026-10-22: two Reversal Engine BUYs were sent 1.4s apart at
    10:55 with the switch on. The first had not been confirmed, so it had no
    row, and the guard saw nothing open. An order in flight has an unknown
    outcome, and unknown is not safe."""

    @pytest.fixture(autouse=True)
    def _on(self, fresh_db):
        _settings(never_add_to_loser_enabled=1)

    def test_a_same_direction_order_in_flight_refuses_the_next(self):
        _in_flight("ra25e34")
        with pytest.raises(ValueError, match="in flight"):
            _check(_Bridge(_tick(4081.29, 4081.50)), signal_id="r7761a4")

    def test_an_order_in_flight_the_other_way_does_not_refuse(self):
        _in_flight("ra25e34", direction="SELL")
        _check(_Bridge(_tick(4081.29, 4081.50)), signal_id="r7761a4")

    def test_its_own_claim_does_not_refuse_the_order_being_opened(self):
        _in_flight("r7761a4")
        _check(_Bridge(_tick(4081.29, 4081.50)), signal_id="r7761a4")

    def test_a_resting_limit_order_is_not_an_order_in_flight(self):
        """Its signal stays `activating` until the fill; that is a resting
        order, as max open trades already treats it."""
        _in_flight("ra25e34", resting=True)
        _check(_Bridge(_tick(4081.29, 4081.50)), signal_id="r7761a4")

    def test_once_recorded_it_is_judged_on_its_price_instead(self):
        """The row exists and is in profit; the claim not yet flipped to
        active must not refuse on its own."""
        _in_flight("ra25e34")
        _row("t1", "BUY", 4080.00, 4075.00, signal_id="ra25e34x")
        with db.db() as conn:
            conn.execute("UPDATE vantage_simulated_trades SET signal_id='ra25e34' "
                         "WHERE trade_id='t1'")
        _check(_Bridge(_tick(4081.29, 4081.50)), signal_id="r7761a4")

    def test_switched_off_an_order_in_flight_refuses_nothing(self):
        _settings(never_add_to_loser_enabled=0)
        _in_flight("ra25e34")
        _check(_Bridge(_tick(4081.29, 4081.50)), signal_id="r7761a4")
