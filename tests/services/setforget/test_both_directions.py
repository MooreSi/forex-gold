"""Set & Forget looks for a BUY and a SELL, not only the higher-timeframe side.

Owner, 2026-10-02, with a screenshot: the page "only ever shows a sell which it
never reaches"; it should look for both, and inverted there was a setup. The
live read that morning, reproduced in `LIVE_2026_10_02` below: Weekly and Daily
both bearish, so `propose` built only the SELL -- armed at the 4330 supply, 143
points above price -- while the 4103-4130 demand 57 points below had held
twice that week. Replaying the page's rules over 22 Sep - 2 Oct, the BUY at
that demand TRIGGERED on 30 Sep 23:00 and 1 Oct 13:00 and was never shown.

Auto had the mirror problem: hard-wired to longs, so in a falling market every
long it found was counter-trend and the AI declined all four (the app log,
1-2 Oct), while the with-trend SELLs that triggered on 22, 23 and 25 Sep were
never looked at.

`analysis.scan` builds both sides with `propose(direction=...)` -- every rule
still applies to each -- and ranks them: placeable first, then the furthest
stage, then the Weekly/Daily side, then the nearest. The other side travels
with it so the page can show what else is being watched.
"""
from __future__ import annotations

import pytest

from backend.src.services.setforget import analysis, auto, setup

from ._candles import series


def _zone(kind, low, high, touches=3):
    return {"kind": kind, "low": low, "high": high, "ts": 0.0, "touches": touches}


# The live read of 2026-10-02 09:00 UTC, from GET /api/trading/setforget.
LIVE_2026_10_02 = {
    "price": 4187.51, "weekly_bias": "bearish", "daily_bias": "bearish",
    "entry_bias": "ranging",
    "zones": [_zone("demand", 3318.22, 3349.86, 4),
              _zone("demand", 4102.85, 4129.89),
              _zone("supply", 4330.48, 4353.3)],
    "atr": 33.17, "daily_atr": 83.40, "confirmation": None,
    "trigger_candles": [],
}

# Falls, bounces to a swing high, falls again, then closes back through it.
TURNED_UP = [118, 112, 106, 120, 108, 102, 98, 104, 112, 126]
# The mirror: rises, dips to a swing low, rises again, then closes under it.
TURNED_DOWN = [82, 88, 94, 80, 92, 98, 102, 96, 88, 74]
QUIET = [118, 112, 106, 120, 108, 102, 98, 96, 94]


def _ev(**over) -> dict:
    ev = {"price": 2000.0, "weekly_bias": "bullish", "daily_bias": "bullish",
          "entry_bias": "bullish",
          "zones": [_zone("demand", 1975.0, 1985.0),
                    _zone("supply", 2040.0, 2050.0)],
          "atr": 6.0, "daily_atr": 20.0, "confirmation": None,
          "trigger_candles": []}
    ev.update(over)
    return ev


class TestTheLiveRead:
    def test_the_faithful_read_alone_offered_only_the_distant_sell(self):
        """The control: what the page showed. If this stops being true the
        test below proves nothing about the defect."""
        candidate, _ = analysis.propose(dict(LIVE_2026_10_02))

        assert candidate["direction"] == "SELL"
        assert candidate["stage"] == "armed"
        assert candidate["distance"] == pytest.approx(142.97, abs=0.01)

    def test_the_scan_shows_the_buy_price_is_nearer_to(self):
        best, why, other = analysis.scan(dict(LIVE_2026_10_02))

        assert best is not None, why
        assert best["direction"] == "BUY"
        assert best["entry"] == pytest.approx(4129.89)
        assert best["take_profit"] == pytest.approx(4330.48)
        assert other is not None and other["direction"] == "SELL"


class TestTheRanking:
    def test_a_triggered_side_outranks_an_armed_one_even_against_the_bias(self):
        """Bullish Weekly and Daily, but price is at the supply and the 30m has
        turned down: the SELL is placeable now, the BUY is days of waiting."""
        best, why, other = analysis.scan(_ev(
            price=2045.0, trigger_candles=series(TURNED_DOWN)))

        assert best is not None, why
        assert best["direction"] == "SELL"
        assert best["stage"] == "triggered"
        assert setup.invalidations(best) == []
        assert other["direction"] == "BUY" and other["stage"] == "armed"

    def test_at_the_same_stage_the_higher_timeframe_side_wins(self):
        """Both sides waiting at once (price between two adjacent zones): the
        method's own direction is the one shown."""
        ev = _ev(weekly_bias="bearish", daily_bias="bearish", price=2000.0,
                 zones=[_zone("demand", 1990.0, 1998.0),
                        _zone("supply", 2002.0, 2010.0)],
                 trigger_candles=series(QUIET))

        best, why, other = analysis.scan(ev)

        assert best is not None, why
        assert (best["stage"], other["stage"]) == ("waiting", "waiting")
        assert best["direction"] == "SELL"

    def test_when_only_one_side_can_be_built_it_is_shown_alone(self):
        """The supply is 70 points up, 3.5 daily ranges: too far to rest a
        short at, but still the long's target."""
        best, why, other = analysis.scan(_ev(
            zones=[_zone("demand", 1975.0, 1985.0),
                   _zone("supply", 2070.0, 2080.0)],
            weekly_bias="bearish", daily_bias="bullish"))

        assert best is not None, why
        assert best["direction"] == "BUY"
        assert other is None

    def test_neither_side_names_both_reasons(self):
        best, why, other = analysis.scan(_ev(zones=[]))

        assert best is None and other is None
        assert "demand" in why and "supply" in why

    def test_no_price_is_still_named(self):
        best, why, _ = analysis.scan(_ev(price=None))

        assert best is None
        assert "no price" in why.lower()


class TestTheChecklistDoesNotClaimArrival:
    def test_an_armed_candidate_is_not_scored_as_at_the_zone(self):
        """The 2026-10-02 page scored "Price is at a supply zone 4330.48-4353.30"
        as passed with price 143 points below it -- 2 of the 7 points on a
        "high" grade, for a zone price had not reached."""
        ev = dict(LIVE_2026_10_02)
        candidate, _ = analysis.propose(ev)
        assert candidate["stage"] == "armed"

        items = {i["id"]: i for i in analysis.score(ev, candidate)["items"]}

        assert items["at_aoi"]["passed"] is False

    def test_a_candidate_at_the_zone_still_scores_it(self):
        ev = _ev(price=1980.0, trigger_candles=series(QUIET))
        candidate, _ = analysis.propose(ev)
        assert candidate["stage"] == "waiting"

        items = {i["id"]: i for i in analysis.score(ev, candidate)["items"]}

        assert items["at_aoi"]["passed"] is True


class _Engine:
    """Sentinel runtime: records every call, places nothing."""

    def __init__(self):
        self.calls: list[tuple[str, dict]] = []

    async def get_mt5_account(self):
        return {"balance": 10_000.0}

    async def open_manual_market_order(self, **kwargs):
        self.calls.append(("open_manual_market_order", kwargs))
        return {"trade_id": "t1", "mt5_ticket": 42}


@pytest.fixture
def auto_world(monkeypatch):
    """Demo, Auto on, nothing open, the AI says take. The scan is REAL; only
    the chart read is replaced, with a bullish chart whose 30m has turned down
    at the supply -- a placeable short and nothing placeable long."""
    async def gather(engine):
        return _ev(price=2045.0, trigger_candles=series(TURNED_DOWN))

    async def review(evidence, candidate, cfg, timeout=60):
        return {"ai": {"verdict": "take", "reasoning": "", "error": None},
                "candidate": candidate, "billed": True}

    monkeypatch.setattr(auto._analysis, "gather", gather)
    monkeypatch.setattr(auto._analysis, "review", review)
    monkeypatch.setattr(auto._ai, "is_configured", lambda cfg: True)
    monkeypatch.setattr(auto, "is_enabled", lambda: True)
    monkeypatch.setattr(auto._repo, "trades_today", lambda source, since: 0)
    monkeypatch.setattr(auto._repo, "open_positions", lambda source: 0)
    monkeypatch.setattr(auto._risk, "get", lambda: {"risk_per_trade_pct": 1.0})
    monkeypatch.setattr(auto._setup, "lot_from_risk", lambda *a: 0.01)


@pytest.mark.asyncio
class TestAutoTradesEitherWay:
    async def test_a_triggered_short_is_placed_as_a_short(self, auto_world):
        eng = _Engine()

        out = await auto.tick(eng, {"account_env": "demo"})

        assert out["decision"] == "placed", out
        (name, kw), = eng.calls
        assert kw["direction"] == "SELL"
        assert kw["stop_loss"] > 2045.0 > kw["take_profit"]
        assert out["reason"].startswith("Sold")

    async def test_live_is_still_refused_whichever_way(self, auto_world):
        eng = _Engine()

        out = await auto.tick(eng, {"account_env": "live"})

        assert out["decision"] == "refused"
        assert eng.calls == []
