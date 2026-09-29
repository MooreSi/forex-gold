"""An instant-entry message whose "price" is only punctuation is a market entry.

`_INSTANT_RE` captured the optional price as `[\\d.,]+`, which matches a lone
"." -- so "XAUUSD BUY NOW." handed "." to float() and raised. scan_messages
logs and skips a message that raises, before the dedup row that marks it
processed is written, so the same message was re-parsed and re-logged about
once a second for as long as it stayed in the reader's window: 117 tracebacks
for tg_id=22147 in the VPS's last 3,000 log lines on 2026-09-28.

The message is the bare trigger the parser exists for; the full stop is
sentence punctuation, not a price.
"""
from __future__ import annotations

import pytest

from backend.src.services.signals.parser import parse_instant_entry


@pytest.mark.parametrize("text", [
    "XAUUSD BUY NOW.",
    "XAUUSD Buy Now .",
    "XAU SELL NOW,",
    "XAU USD BUY NOW ...",
])
def test_punctuation_after_now_is_a_market_entry(text):
    direction, price = parse_instant_entry(text)

    assert direction in ("BUY", "SELL")
    assert price is None


@pytest.mark.parametrize("text, expected", [
    ("XAUUSD Buy Now 4293", 4293.0),
    ("XAUUSD Buy Now 4293.", 4293.0),
    ("XAUUSD Buy Now @ 4293.50", 4293.5),
    ("XAU SELL NOW 4,293.50", 4293.5),
])
def test_a_real_price_is_still_read(text, expected):
    """Negative control: the fix must not turn a limit price into a market
    order."""
    _direction, price = parse_instant_entry(text)

    assert price == expected
