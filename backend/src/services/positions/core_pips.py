"""Pip conversion for this app's XAUUSD feed.

1 pip = 0.10 price = 10 * _Point (_Point is 0.01 on this feed). Confirmed
two ways: the reference channels' own wording ("TP1 HIT +30 PIPS
(4076 TO 4079)" = 3.0 of price, ratio 10:1 across two dozen messages checked
2026-07-31) and dollar-risk sizing math elsewhere in the app. Mirrors
ForexTraderBridge.mq5's PipsToPrice(), which already applies this correctly
for EA-side pending-leg TPs and trailing distances -- this is the Python-side
counterpart, needed wherever a *_pips template field is converted to an
absolute price before being sent to the EA (rather than sent as pips for the
EA to convert itself).

Getting this wrong scales every stop and target by 10x.
"""
from __future__ import annotations

PIPS_TO_PRICE_XAUUSD = 0.10


def pips_to_price(pips: float) -> float:
    return pips * PIPS_TO_PRICE_XAUUSD
