"""The Trend PA replay. Pure: candle lists in, trades out.

The one property that decides whether any number it produces means anything:
at every step the strategy sees only bars that had CLOSED by then. The
Reversal engine's first entry study filled inside the bar its signal was
created in, and the cohort it flattered read z = +4.7 until that was fixed
(engines README, 2026-09-24).
"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.src.services.trend_pa import backtest as bt
from backend.src.services.trend_pa import strategy as st

M15, H1, H4 = 900, 3600, 14400
# A Tuesday, 00:00 broker time.
T0 = int(datetime(2026, 9, 1, 0, 0, tzinfo=timezone.utc).timestamp())


def _series(step, n, start=T0, price=2000.0):
    return [{"ts": start + i * step, "open": price, "high": price + 1,
             "low": price - 1, "close": price} for i in range(n)]


def _setup(entry=2000.0, sl=1998.0, tp=2004.0, direction="BUY"):
    return st.Setup(direction=direction, pattern="pin", entry=entry, stop_loss=sl,
                    take_profit=tp, risk=abs(entry - sl), level=1999.0,
                    level_kind="swing_low", session="london", atr_m15=2.0,
                    atr_h4=8.0, features={"x": 1.0})


def test_the_strategy_never_sees_a_bar_that_had_not_closed(monkeypatch):
    seen = []

    def spy(h4, h1, m15, now_utc, params=None):
        now = now_utc.timestamp() + bt.BROKER_OFFSET_S
        seen.append(now)
        for bars, step in ((h4, H4), (h1, H1), (m15, M15)):
            assert bars, "the replay handed over an empty window"
            assert bars[-1]["ts"] + step <= now, "a bar still forming was visible"
        return "nothing"
    monkeypatch.setattr(bt.st, "evaluate", spy)

    # The higher timeframes run on PAST the M15 window, so a replay that
    # peeked would have future H1/H4 bars to see. Ending them before the
    # window (as this test first did) let exactly that mutant survive.
    bt.run(_series(H4, 450, start=T0 - 400 * H4), _series(H1, 600, start=T0 - 400 * H1),
           _series(M15, 200))
    assert len(seen) > 100


def test_the_newest_closed_bar_is_the_one_handed_over(monkeypatch):
    """The opposite failure: a replay that lags a bar is also wrong -- it
    trades on stale candles and the result is about a different strategy."""
    lags = []

    def spy(h4, h1, m15, now_utc, params=None):
        now = now_utc.timestamp() + bt.BROKER_OFFSET_S
        lags.append(now - (m15[-1]["ts"] + M15))
        return "nothing"
    monkeypatch.setattr(bt.st, "evaluate", spy)
    bt.run(_series(H4, 400, start=T0 - 400 * H4), _series(H1, 400, start=T0 - 400 * H1),
           _series(M15, 100))
    assert set(lags) == {0}


def test_a_setup_becomes_a_trade_resolved_on_later_bars_only(monkeypatch):
    m15 = _series(M15, 60)
    fire_at = 30
    m15[fire_at]["low"] = 1990.0          # would stop the trade out IF it counted
    m15[fire_at + 3]["high"] = 2005.0      # the target, three bars later

    def once(h4, h1, m15_w, now_utc, params=None):
        return _setup() if m15_w[-1]["ts"] == m15[fire_at]["ts"] else "no"
    monkeypatch.setattr(bt.st, "evaluate", once)

    trades = bt.run(_series(H4, 400, start=T0 - 400 * H4),
                    _series(H1, 400, start=T0 - 400 * H1), m15, cost=0.0)
    assert len(trades) == 1
    t = trades[0]
    assert t["outcome"] == "win" and t["exit_ts"] == m15[fire_at + 3]["ts"]
    assert t["r_net"] == pytest.approx(2.0)


def test_one_trade_at_a_time(monkeypatch):
    monkeypatch.setattr(bt.st, "evaluate", lambda *a, **k: _setup(tp=2100.0, sl=1900.0))
    m15 = _series(M15, 80)
    trades = bt.run(_series(H4, 400, start=T0 - 400 * H4),
                    _series(H1, 400, start=T0 - 400 * H1), m15, max_hold_s=10 * M15)
    for a, b in zip(trades, trades[1:]):
        assert b["created_at"] >= a["exit_ts"]


def test_costs_are_charged(monkeypatch):
    m15 = _series(M15, 40)
    m15[21]["high"] = 2005.0

    def once(h4, h1, m15_w, now_utc, params=None):
        return _setup() if m15_w[-1]["ts"] == m15[20]["ts"] else "no"
    monkeypatch.setattr(bt.st, "evaluate", once)
    t = bt.run(_series(H4, 400, start=T0 - 400 * H4),
               _series(H1, 400, start=T0 - 400 * H1), m15, cost=0.3)[0]
    assert t["r_net"] == pytest.approx((4.0 - 0.3) / 2.0)


def test_refusal_reasons_are_counted(monkeypatch):
    monkeypatch.setattr(bt.st, "evaluate", lambda *a, **k: "no clear H4 trend")
    reasons: dict = {}
    bt.run(_series(H4, 400, start=T0 - 400 * H4), _series(H1, 400, start=T0 - 400 * H1),
           _series(M15, 50), reasons=reasons)
    assert reasons.get("no clear H4 trend", 0) > 0
