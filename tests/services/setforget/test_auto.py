"""Set & Forget Auto -- the one unattended path in this domain (2026-09-24).

The owner asked for an "Auto" button: every 15 minutes review the market with
the configured AI, look for a LONG setup, and if there is one, execute it. His
choices, 2026-09-24: buy at any validated demand zone whatever the
higher-timeframe bias, the AI decides; at most two Auto trades a day; one open
at a time.

What these tests hold it to, because it places orders with nobody watching:

- It trades on the DEMO account only. Live needs the owner's sign-off and a
  demo session first (CLAUDE.md, rules/20).
- Only a TRIGGERED setup that passes every rule, and only when the AI says
  "take" (or "adjust" with levels that pass the rules again).
- No AI configured means no trade -- the owner's instruction is that the AI
  decides.
- It places through `engine.open_manual_market_order`, the same order path the
  page's Execute button reaches. No second order path.

**No test here reaches a broker.** The engine is a sentinel that records calls.
"""
from __future__ import annotations

import pytest

from backend.src.services.setforget import analysis, auto, setup


class _Engine:
    """Sentinel runtime: records every call, places nothing."""

    def __init__(self, balance: float = 10_000.0, fail: Exception | None = None):
        self.calls: list[tuple[str, dict]] = []
        self.balance = balance
        self.fail = fail

    async def get_mt5_account(self):
        return {"balance": self.balance}

    async def open_manual_market_order(self, **kwargs):
        self.calls.append(("open_manual_market_order", kwargs))
        if self.fail:
            raise self.fail
        return {"trade_id": "t1", "mt5_ticket": 42, "entry_price": kwargs.get("stop_loss")}


def _candidate(stage="triggered", entry=4300.0, stop=4280.0, target=4380.0):
    c = setup.build("BUY", entry, stop, target, order_type="market")
    c["stage"] = stage
    c["zone"] = {"kind": "demand", "low": 4285.0, "high": 4300.0}
    c["target_zone"] = {"kind": "supply", "low": 4380.0, "high": 4400.0}
    return c


@pytest.fixture
def world(monkeypatch):
    """A demo account with Auto on, no trades today, and a triggered long the
    AI approves. Each test changes one thing."""
    state = {
        "enabled": True, "trades_today": 0, "open": 0,
        "candidate": _candidate(), "why": "",
        "ai": {"verdict": "take", "reasoning": "clean", "error": None},
        "revised": None,
        "configured": True,
        "gathered": 0,
        "risk": {"risk_per_trade_pct": 1.0, "setforget_lot_size": 0.0},
    }

    async def gather(engine):
        state["gathered"] += 1
        return {"price": 4300.0, "atr": 10.0, "weekly_bias": "bearish",
                "daily_bias": "bearish"}

    def propose(evidence, direction=None):
        assert direction == "BUY", "Auto looks for longs only"
        return state["candidate"], state["why"]

    async def review(evidence, candidate, cfg, timeout=60):
        return {"ai": state["ai"], "candidate": state["revised"] or candidate,
                "billed": True}

    monkeypatch.setattr(auto._analysis, "gather", gather)
    monkeypatch.setattr(auto._analysis, "propose", propose)
    monkeypatch.setattr(auto._analysis, "review", review)
    monkeypatch.setattr(auto._ai, "is_configured", lambda cfg: state["configured"])
    monkeypatch.setattr(auto, "is_enabled", lambda: state["enabled"])
    monkeypatch.setattr(auto._repo, "trades_today", lambda source, since: state["trades_today"])
    monkeypatch.setattr(auto._repo, "open_positions", lambda source: state["open"])
    monkeypatch.setattr(auto._risk, "get", lambda: state["risk"])
    # The real sizer reads Global Parameters from the database. 0.01 lots is
    # what it returns for a small account, and keeps these tests off the DB.
    monkeypatch.setattr(auto._setup, "lot_from_risk", lambda *a: 0.01)
    return state


DEMO = {"account_env": "demo"}


@pytest.mark.asyncio
class TestItTrades:
    async def test_a_triggered_long_the_ai_takes_is_placed_once(self, world):
        eng = _Engine()

        out = await auto.tick(eng, DEMO)

        assert out["decision"] == "placed", out
        (name, kw), = eng.calls
        assert name == "open_manual_market_order"
        assert kw["direction"] == "BUY"
        assert kw["stop_loss"] == 4280.0
        assert kw["take_profit"] == 4380.0
        assert kw["source_name"] == auto.SOURCE_NAME
        assert kw["strategy"] == "set_and_forget"

    async def test_the_lot_is_sized_by_the_shared_sizer(self, world, monkeypatch):
        """No sizing maths of its own: the page's lot, else the app's one
        risk-based sizer, fed this trade's entry, stop and the real balance."""
        seen = []

        def sizer(entry, stop, balance, risk_pct):
            seen.append((entry, stop, balance, risk_pct))
            return 0.05

        monkeypatch.setattr(auto._setup, "lot_from_risk", sizer)
        eng = _Engine(balance=10_000.0)

        await auto.tick(eng, DEMO)

        assert seen == [(4300.0, 4280.0, 10_000.0, 1.0)]
        assert eng.calls[0][1]["lot_size"] == pytest.approx(0.05)

    async def test_the_pages_own_lot_size_wins_when_it_is_set(self, world):
        world["risk"] = {"risk_per_trade_pct": 1.0, "setforget_lot_size": 0.02}
        eng = _Engine()

        await auto.tick(eng, DEMO)

        assert eng.calls[0][1]["lot_size"] == pytest.approx(0.02)

    async def test_an_adjusted_answer_trades_the_ais_revalidated_levels(self, world):
        world["ai"] = {"verdict": "adjust", "reasoning": "tighter", "error": None}
        world["revised"] = _candidate(stop=4288.0, target=4360.0)
        eng = _Engine()

        await auto.tick(eng, DEMO)

        kw = eng.calls[0][1]
        assert (kw["stop_loss"], kw["take_profit"]) == (4288.0, 4360.0)


@pytest.mark.asyncio
class TestItRefuses:
    async def _refused(self, world, **env) -> dict:
        eng = _Engine(**env)
        out = await auto.tick(eng, DEMO if "live" not in env else env)
        assert eng.calls == [], out
        assert out["decision"] != "placed"
        return out

    async def test_nothing_happens_while_auto_is_off(self, world):
        world["enabled"] = False
        eng = _Engine()

        out = await auto.tick(eng, DEMO)

        assert eng.calls == [] and world["gathered"] == 0
        assert out["decision"] == "off"

    async def test_it_never_trades_the_live_account(self, world):
        eng = _Engine()

        out = await auto.tick(eng, {"account_env": "live"})

        assert eng.calls == []
        assert "demo" in out["reason"].lower()

    async def test_an_unknown_account_is_treated_as_live(self, world):
        eng = _Engine()

        out = await auto.tick(eng, {})

        assert eng.calls == [] and out["decision"] == "refused"

    async def test_two_auto_trades_today_is_the_cap(self, world):
        world["trades_today"] = auto.MAX_TRADES_PER_DAY
        out = await self._refused(world)
        assert "today" in out["reason"]

    async def test_one_open_auto_position_blocks_another(self, world):
        world["open"] = 1
        out = await self._refused(world)
        assert "open" in out["reason"]

    async def test_no_candidate_is_no_trade_with_the_rules_reason(self, world):
        world["candidate"], world["why"] = None, "There is no demand zone below price."
        out = await self._refused(world)
        assert out["reason"] == "There is no demand zone below price."

    @pytest.mark.parametrize("stage", ["armed", "waiting"])
    async def test_a_setup_that_has_not_triggered_is_not_placed(self, world, stage):
        world["candidate"] = _candidate(stage=stage)
        await self._refused(world)

    async def test_a_setup_that_breaks_a_rule_is_not_placed(self, world):
        world["candidate"] = _candidate(target=4310.0)   # 1:0.5
        out = await self._refused(world)
        assert "1:" in out["reason"]

    async def test_an_adjusted_entry_away_from_price_is_not_bought_at_market(
            self, world):
        """Auto buys at the market. An AI entry $15 lower is a resting order
        with a different ratio, so its levels do not describe this trade."""
        world["ai"] = {"verdict": "adjust", "reasoning": "wait lower", "error": None}
        revised = _candidate(entry=4285.0, stop=4270.0, target=4380.0)
        revised["order_type"] = "limit"
        world["revised"] = revised
        out = await self._refused(world)
        assert "entry" in out["reason"]

    async def test_the_ai_saying_skip_is_no_trade(self, world):
        world["ai"] = {"verdict": "skip", "reasoning": "no", "error": None}
        await self._refused(world)

    async def test_an_ai_that_did_not_answer_is_no_trade(self, world):
        world["ai"] = {"verdict": None, "error": "timeout"}
        await self._refused(world)

    async def test_no_ai_configured_is_no_trade(self, world):
        world["configured"] = False
        out = await self._refused(world)
        assert "AI" in out["reason"]

    async def test_a_stop_that_risks_too_much_of_the_account_is_refused(self, world):
        """0.01 lots over a 20-point stop is $20: 10% of a $200 account."""
        eng = _Engine(balance=200.0)

        out = await auto.tick(eng, DEMO)

        assert eng.calls == []
        assert "%" in out["reason"]

    async def test_a_broker_refusal_is_reported_not_raised(self, world):
        eng = _Engine(fail=ValueError("Trading stood down"))

        out = await auto.tick(eng, DEMO)

        assert out["decision"] == "failed"
        assert "Trading stood down" in out["reason"]


class TestTheSwitch:
    def test_it_is_off_until_it_is_turned_on(self, monkeypatch):
        store: dict = {}
        monkeypatch.setattr(auto._config, "get_app_config", store.get)
        monkeypatch.setattr(auto._config, "set_app_config",
                            lambda k, v: store.__setitem__(k, v))

        assert auto.is_enabled() is False
        auto.set_enabled(True)
        assert auto.is_enabled() is True
        auto.set_enabled(False)
        assert auto.is_enabled() is False


class TestLongsWhateverTheBias:
    def test_a_forced_direction_ignores_disagreeing_higher_timeframes(self):
        """The owner's rule for Auto: any validated demand zone. The page's own
        read keeps the method's bias gate -- only Auto passes a direction."""
        ev = {
            "price": 4300.0, "weekly_bias": "bearish", "daily_bias": "bullish",
            "zones": [{"kind": "demand", "low": 4285.0, "high": 4298.0, "ts": 0,
                       "touches": 3},
                      {"kind": "supply", "low": 4380.0, "high": 4390.0, "ts": 0,
                       "touches": 3}],
            "atr": 10.0, "daily_atr": 80.0, "confirmation": None,
            "trigger_candles": [],
        }

        free, why = analysis.propose(ev)
        forced, _ = analysis.propose(ev, direction="BUY")

        assert free is None and "disagree" in why
        assert forced is not None and forced["direction"] == "BUY"


class TestWhenAScanIsDue:
    def test_the_first_pass_after_switching_on_scans_at_once(self):
        assert auto.scan_due(1000.0, None) is True

    def test_then_every_fifteen_minutes(self):
        assert auto.scan_due(1000.0 + auto.INTERVAL_S - 1, 1000.0) is False
        assert auto.scan_due(1000.0 + auto.INTERVAL_S, 1000.0) is True

    def test_the_interval_is_fifteen_minutes(self):
        assert auto.INTERVAL_S == 15 * 60
