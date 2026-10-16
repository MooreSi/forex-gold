"""A window with winners and no losers says so, instead of a profit factor of
0.00 (owner, 2026-10-16).

On a day the daily goal was reached with only winning trades, the dashboard's
Trading performance card read PROFIT FACTOR 0.00, in red. The figure has no
finite value there and `compute_mt5_performance` returns 0.0 for it, which
tests/core/test_mt5_history_characterization.py pins and is left alone. The
payload now also carries `no_losses`, and every screen that shows the figure
reads it: the dashboard and History cards and both emails.

Nothing here reaches a broker: the bridge is a fake returning canned deals.
"""
from __future__ import annotations

import asyncio
import time

from backend.src.services.broker.mt5_performance import compute_mt5_performance
from backend.src.services.notifications import email_html


class _Bridge:
    def __init__(self, profits):
        now = time.time()
        self._deals = []
        for i, p in enumerate(profits, start=1):
            self._deals += [
                {"position_id": i, "profit": 0.0, "entry": 0, "time": now - 3600,
                 "volume": 0.10, "type": 0, "price": 2400.0, "swap": 0.0, "fee": 0.0,
                 "comment": ""},
                {"position_id": i, "profit": p, "entry": 1, "time": now - 1800,
                 "volume": 0.10, "type": 1, "price": 2401.0, "swap": 0.0, "fee": 0.0,
                 "comment": ""},
            ]

    async def get_account(self):
        return {"balance": 1000.0, "equity": 1000.0}

    async def get_deal_history(self, days: int = 7):
        return self._deals

    async def get_positions(self):
        return []


def _perf(profits):
    return asyncio.run(compute_mt5_performance(_Bridge(profits), 90))


def test_only_winners_is_no_losses(fresh_db):
    assert _perf([12.5, 17.07])["no_losses"] is True


def test_a_loser_is_not_no_losses(fresh_db):
    perf = _perf([100.0, -50.0])
    assert perf["no_losses"] is False
    assert perf["profit_factor"] == 2.0


def test_only_losers_is_not_no_losses(fresh_db):
    """No winners keeps the 0.00, which is the true figure there."""
    perf = _perf([-10.0])
    assert perf["no_losses"] is False
    assert perf["profit_factor"] == 0.0


def test_no_trades_is_not_no_losses(fresh_db):
    assert _perf([])["no_losses"] is False


# ── the emails ───────────────────────────────────────────────────────────────

def _cell(html: str) -> str:
    """The text of the PROFIT FACTOR stat cell."""
    return html.split("PROFIT FACTOR", 1)[1].split("</td>", 1)[0]


def test_the_weekly_email_says_no_losses(fresh_db):
    html = email_html.build_weekly_html(
        {"profit_factor": 0.0, "no_losses": True}, [])
    assert "No losses" in _cell(html)
    assert "0.00" not in _cell(html)


def test_the_weekly_email_keeps_a_real_figure(fresh_db):
    html = email_html.build_weekly_html({"profit_factor": 2.0, "no_losses": False}, [])
    assert "2.00" in _cell(html)


def test_the_daily_email_says_no_losses_for_an_all_winners_day(fresh_db):
    """It computes today's figure itself, from the day's closes. Before this
    an all-winners day divided by $0.01 and printed thousands."""
    html = email_html.build_daily_html({}, [{"net_pnl": 29.57}, {"net_pnl": 4.10}])
    assert "No losses" in _cell(html)


def test_the_daily_email_keeps_a_real_figure(fresh_db):
    html = email_html.build_daily_html({}, [{"net_pnl": 30.0}, {"net_pnl": -10.0}])
    assert "3.00" in _cell(html)


def test_the_daily_email_with_no_closes_is_zero(fresh_db):
    assert "0.00" in _cell(email_html.build_daily_html({}, []))
