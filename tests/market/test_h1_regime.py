"""H1 regime: trend or range, from how efficiently price travelled over the
last few hours (docs/todo/014).

The classifier Auto used (30 M5 bars, ADX bands) called about 60% of both
5 October (choppy, net -$8 on a $47 range) and 6 October (rally from 4113 to
4179) "trending". The efficiency ratio -- |net move| / the sum of every bar's
move -- separates them: 0.05 against 0.21 over the whole day.
"""
from tools import auto_template_study as hr


def _bars(closes, start=4100.0):
    out, o = [], start
    for i, c in enumerate(closes):
        out.append({"ts": i * 3600.0, "open": o, "high": max(o, c) + 1,
                    "low": min(o, c) - 1, "close": c})
        o = c
    return out


RALLY = _bars([4105, 4110, 4116, 4120, 4127, 4133])        # +33, every bar up
ZIGZAG = _bars([4108, 4099, 4109, 4098, 4107, 4101])        # +1 on a long path
SELLOFF = _bars([4095, 4090, 4083, 4079, 4072, 4066])


def test_a_steady_rally_is_an_uptrend():
    r = hr.read(RALLY, hours=6, threshold=0.4)
    assert (r.regime, r.direction) == ("trend", "up")
    assert r.er == 1.0


def test_a_zigzag_is_a_range():
    r = hr.read(ZIGZAG, hours=6, threshold=0.4)
    assert r.regime == "range"
    assert r.er < 0.1


def test_a_selloff_is_a_downtrend():
    assert hr.read(SELLOFF, hours=6, threshold=0.4).direction == "down"


def test_only_the_last_n_hours_count():
    r = hr.read(ZIGZAG + _bars([4110, 4120, 4130], start=4101), hours=3, threshold=0.4)
    assert r.regime == "trend"


def test_too_few_bars_cannot_say():
    assert hr.read(RALLY[:3], hours=6, threshold=0.4) is None


def test_a_flat_market_is_a_range():
    assert hr.read(_bars([4100] * 6), hours=6, threshold=0.4).regime == "range"


def test_side_of_a_trade():
    up = hr.read(RALLY, hours=6, threshold=0.4)
    rng = hr.read(ZIGZAG, hours=6, threshold=0.4)
    assert hr.side(up, "BUY") == "with"
    assert hr.side(up, "SELL") == "against"
    assert hr.side(rng, "SELL") == "flat"
    assert hr.side(None, "BUY") == "flat"
