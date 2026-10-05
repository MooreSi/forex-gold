"""ORB measures its reward from a price it can actually enter at (bugs/059).

2026-09-16: stop and target were anchored to the opening-range edge
(4336.21), but a bullish breakout only confirms above the Asian high
(4341.03). The report said 2.00:1; at the earliest possible fill it was
0.17:1. And under BST the "Asian" confirmation range ran to 08:00 UTC, an
hour past London's 07:00 UTC open, so it absorbed the breakout move itself.

Fixed (owner approval 2026-10-05, provisional policy): the report carries the
realised R:R at the current price; auto-execute refuses below 1:1, the same
way it already refuses a target that has been passed; the Asian range ends
where the opening range begins. The 2:1 geometry from the edge is unchanged.

No real or demo order: open_manual_market_order is a mock.
"""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest import mock

from backend.src.services.analytics import orb_report as orb
from backend.src.services.trading import orb_execute as orb_exec

from tests.core.test_orb_report_surface import _patched_now, _patch_open_market

# 2026-09-16's numbers.
_SEPT16 = {"direction": "bullish", "stop": 4333.11, "target": 4342.41,
           "target2": 4345.51, "current_price": 4341.03}


def test_the_september_16_setup_is_refused(fresh_db):
    patcher, m = _patch_open_market()
    with patcher, mock.patch("backend.src.services.telegram.alerts.send_message"):
        asyncio.run(orb_exec.orb_auto_execute(dict(_SEPT16), None, True))
    m.assert_not_called()


def test_a_fill_near_the_edge_still_trades(fresh_db):
    report = dict(_SEPT16, current_price=4336.50)   # 5.91 to target, 3.39 to stop
    patcher, m = _patch_open_market()
    with patcher:
        asyncio.run(orb_exec.orb_auto_execute(report, None, True))
    m.assert_called_once()


def test_bearish_mirror_is_refused(fresh_db):
    report = {"direction": "bearish", "stop": 4333.11, "target": 4320.71,
              "current_price": 4322.0}               # 1.29 to target, 11.11 to stop
    patcher, m = _patch_open_market()
    with patcher, mock.patch("backend.src.services.telegram.alerts.send_message"):
        asyncio.run(orb_exec.orb_auto_execute(report, None, True))
    m.assert_not_called()


# Monday 2026-07-20, BST: London opens 07:00 UTC.
_ASIA0 = datetime(2026, 7, 20, 0, 0, tzinfo=timezone.utc).timestamp()
_OR0 = datetime(2026, 7, 20, 7, 0, tzinfo=timezone.utc).timestamp()


class _Bridge:
    def __init__(self, candles, ask):
        self._c, self._ask = candles, ask

    async def get_tick(self):
        return SimpleNamespace(bid=self._ask - 0.5, ask=self._ask)

    async def get_candles_range(self, start, end, timeframe="M1"):
        return [c for c in self._c if start <= c["ts"] < end]


def _candles(post_open_high):
    out, t = [], _ASIA0
    while t < _ASIA0 + 8 * 3600:
        if _OR0 <= t < _OR0 + 900:
            hi, lo = 2402.0, 2398.0
        elif t >= _OR0 + 900:
            hi, lo = post_open_high, 2399.0      # the breakout move, after London opened
        else:
            hi, lo = 2405.0, 2395.0
        out.append({"ts": t, "high": hi, "low": lo, "volume": 5.0})
        t += 60
    return out


def _report(candles, ask, now):
    p = _patched_now(now)
    try:
        return asyncio.run(orb.build_orb_report(_Bridge(candles, ask)))
    finally:
        p.stop()


def test_under_bst_the_asian_range_ends_at_london_open(fresh_db):
    r = _report(_candles(2412.0), 2410.0, datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc))
    assert r["asia_high"] == 2405.0
    assert r["direction"] == "bullish"


def test_the_report_carries_the_realised_rr_at_the_current_price(fresh_db):
    r = _report(_candles(2405.0), 2405.5, datetime(2026, 7, 20, 9, 0, tzinfo=timezone.utc))
    # edge 2402, stop 2400, target 2406: from 2405.5 that is 0.5 up, 5.5 down.
    assert r["rr"] == 2.0
    assert r["realised_rr"] == round(0.5 / 5.5, 2)


def test_the_email_names_the_real_hours_and_the_realised_rr(fresh_db):
    from backend.src.services.notifications.email_html import build_orb_html
    report = {"direction": "bullish", "current_price": 2405.5,
              "asia_high": 2405.0, "asia_low": 2395.0, "asia_range": 10.0,
              "or_high": 2402.0, "or_low": 2398.0, "or_range": 4.0,
              "or_start": _OR0, "or_end": _OR0 + 900,
              "stop": 2400.0, "target": 2406.0, "target2": 2408.0,
              "rr": 2.0, "realised_rr": 0.09}
    html = build_orb_html(report, "Monday")
    assert "London Opening Range (07:00–07:15 UTC)" in html
    assert "Asian Range (00:00–07:00 UTC)" in html
    assert "0.09:1" in html
