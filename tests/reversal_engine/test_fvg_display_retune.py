"""select_display_fvgs' chart-overlay filters, retuned 2026-08-05.

These pin the three rules that decide what the chart draws, on synthetic
candles so the thresholds are exact rather than whatever gold happened to do.
The retune's own justification is measured on live data and recorded in the
function's docstring; what matters here is that each filter still does the
job it claims, since the numbers were already recalibrated once against a
moving reference and will be again."""
from forex_trader.reversal_engine import ict_patterns
from forex_trader.reversal_engine.ict_patterns import (
    atr, detect_fvgs, select_display_fvgs,
)

TF = 900  # M15, matching the overlay's default timeframe


def _bar(i: int, low: float, high: float, close: float | None = None) -> dict:
    return {"ts": 1_700_000_000 + i * TF, "open": low, "high": high,
            "low": low, "close": high if close is None else close}


# Scaled to real XAUUSD M15 (base ~4000, ATR ~10). Not cosmetic: detect_fvgs
# applies its own absolute min_gap_pts=0.5 floor before any ATR-relative rule
# here gets a say, so a toy series around 100.0 with 1.0 ranges cannot express
# a gap that is small in ATR terms but still a real gap.
BASE = 4000.0
RNG = 10.0


def _series(n: int = 40, base: float = BASE, rng: float = RNG) -> list[dict]:
    """Flat bars of height `rng`, so ATR is exactly `rng` and gap sizes
    expressed as a fraction of ATR are exact."""
    return [_bar(i, base, base + rng, base + rng / 2) for i in range(n)]


def _with_bullish_gap(gap: float, n_before: int = 20, n_after: int = 20,
                      base: float = BASE, rng: float = RNG) -> list[dict]:
    """A clean 3-bar bullish FVG of height `gap`, then quiet bars that neither
    test nor break it (they sit above the gap's top)."""
    out = _series(n_before, base, rng)
    top = base + rng                      # candle[i-2].high
    out.append(_bar(n_before, top, top + gap + rng))          # displacement
    out.append(_bar(n_before + 1, top + gap, top + gap + rng))  # candle[i].low = top+gap
    above = top + gap
    out += [_bar(n_before + 2 + i, above, above + rng) for i in range(n_after)]
    return out


def test_gap_smaller_than_old_floor_is_now_drawn():
    """The core of the retune: 0.50xATR sat above the median live-zone height,
    so zones a trader would have drawn were being filtered out."""
    candles = _with_bullish_gap(gap=3.5)
    a = atr(candles)
    raw = detect_fvgs(candles)
    assert raw, "fixture must actually produce an FVG"
    height = raw[0]["top"] - raw[0]["bottom"]
    assert 0.25 * a <= height < 0.50 * a, "fixture must straddle old/new floor"

    assert len(select_display_fvgs(candles, raw)) == 1
    assert select_display_fvgs(candles, raw, min_atr_frac=0.50) == []


def test_gap_below_the_new_floor_is_still_filtered():
    """The floor was lowered, not removed -- a sliver in a wide-range market
    must not be promoted to a level."""
    candles = _with_bullish_gap(gap=1.0)
    assert detect_fvgs(candles), "fixture must produce an FVG"
    assert select_display_fvgs(candles) == []


def test_inverted_zone_is_never_drawn():
    """The dominant filter, deliberately left strict: once price closes clean
    through the far edge the gap has flipped meaning, so drawing it in its
    original direction would be wrong, not just cluttered."""
    candles = _with_bullish_gap(gap=3.5)
    gap = detect_fvgs(candles)[0]
    # Closes just below the gap's far edge: an inversion, not a test. Kept
    # deliberately shallow -- a violent drop would both distort ATR (the size
    # floor is relative to it) and leave a second, genuinely live bearish gap
    # on the way down, so the test would stop being about inversion at all.
    candles.append(_bar(len(candles), gap["bottom"] - 4,
                        gap["bottom"] + 2, gap["bottom"] - 2))
    fvgs = detect_fvgs(candles)
    assert fvgs[0]["inverted"]

    assert select_display_fvgs(candles, fvgs) == []
    kept = select_display_fvgs(candles, fvgs, show_inverted=True)
    assert [f["inverted"] for f in kept] == [True]


def test_tested_zone_stays_drawn():
    """A wick back into the gap is the retracement the zone exists to predict;
    killing the zone on its own signal would be self-defeating."""
    candles = _with_bullish_gap(gap=3.5)
    gap = detect_fvgs(candles)[0]
    # Wick into the gap but close back above its top.
    candles.append(_bar(len(candles), gap["mid"], gap["top"] + RNG, gap["top"] + RNG / 2))
    fvgs = detect_fvgs(candles)
    assert fvgs[0]["filled"] and not fvgs[0]["inverted"]
    assert len(select_display_fvgs(candles, fvgs)) == 1


def test_zone_cap_is_six():
    candles = _series(10)
    fvgs = [{"top": BASE + i, "bottom": BASE - 1 + i, "mid": BASE - 0.5 + i,
             "direction": "bullish", "idx": i, "filled": False, "inverted": False}
            for i in range(12)]
    picked = select_display_fvgs(candles, fvgs, min_atr_frac=0.0)
    assert len(picked) == 6
    # Most recent kept, returned in chart order.
    assert [f["idx"] for f in picked] == [6, 7, 8, 9, 10, 11]


def test_age_window_matches_the_charts_fetch_depth():
    """recent_bars is now the same 300 the chart fetches, so the fetch is the
    single place the age horizon is set. If the overlay's fetch depth changes
    and this does not, zones start vanishing for a reason nobody will find."""
    import inspect
    sig = inspect.signature(ict_patterns.select_display_fvgs)
    assert sig.parameters["recent_bars"].default == 300


def test_old_zone_is_still_dropped_when_the_window_is_exceeded():
    """The window is wider, not gone."""
    candles = _series(400)
    fvgs = [{"top": BASE + 1.0, "bottom": BASE + 0.5, "mid": BASE + 0.75, "direction": "bullish",
             "idx": 10, "filled": False, "inverted": False}]
    assert select_display_fvgs(candles, fvgs, min_atr_frac=0.0) == []
