"""A pendings-only template must still execute a signal that states one price.

Gold Diggers VIP publishes Instant Market Entry signals: a single price
meaning "take this now", so entry_low == entry_high. Auto paired it with
Auto Limit Balanced / Auto Limit Scalp, both anchors=0 pendings=2, and the
pairing cannot execute anything:

  * the EA's anchor loop is `for(a = 1; a <= anchors)`, so with anchors=0 no
    market order is ever placed; and
  * its zone-spanning test is `zoneLow > 0 && zoneHigh > zoneLow`, which a
    zero-width zone fails, so the resting legs fall back to step-staging
    grid_step_pts away from the market and only fill on a retrace.

Live on 2026-08-17: 8 signals, 8 grids staged, zero legs filled, every trade
row left at ticket=0 / entry=0.0 / pnl=0.00 and expired after its 60-minute
life. The channel looked like it was trading and was not.

These tests pin the conversion and, just as importantly, the cases it must
leave alone -- a ranged signal from the same channel still stages the full
resting grid it was tuned for.
"""
import pytest

from backend.src.services.broker import ea_templates as et


def _tpl(**over):
    base = {
        "name": "Auto Limit Balanced", "mode": "grid",
        "anchors": 0, "pendings": 2,
        "sl_pips": 40.0, "lot_anchor": 0.05, "lot_pending": 0.05,
        "tp1_pips": 40.0, "tp2_pips": 80.0, "tp3_pips": 130.0,
    }
    base.update(over)
    return base


# ── the zone test itself (must match the EA's own) ───────────────────────────

@pytest.mark.parametrize("low,high,expected", [
    (4395.0, 4400.0, True),    # a real zone
    (4398.96, 4398.96, False),  # single stated price -- an IME signal
    (0, 0, False),              # no levels at all
    (4400.0, 4395.0, False),    # inverted; high must exceed low
    (0, 4400.0, False),         # zoneLow > 0 is required
    (None, None, False),
])
def test_zone_detection_matches_the_ea_rule(low, high, expected):
    """HandleOpenTemplateGrid uses `zoneLow > 0 && zoneHigh > zoneLow`. If
    Python disagreed about whether a zone exists, it would convert legs the
    EA was going to span anyway, or leave a signal unexecutable."""
    assert et.signal_has_usable_zone(low, high) is expected


# ── the conversion ───────────────────────────────────────────────────────────

def test_single_price_signal_gets_a_market_anchor():
    out = et.apply_market_anchor_for_zoneless_signal(_tpl(), 4398.96, 4398.96)
    assert out["anchors"] == 1, "nothing would fire at market"
    assert out["pendings"] == 1


def test_total_leg_count_is_unchanged_so_exposure_is_not_increased():
    """A leg is CONVERTED, not added. Adding one would size the trade 50%
    larger than the template specifies, which is not a decision this fix is
    entitled to make."""
    for pendings in (1, 2, 3, 5):
        tpl = _tpl(pendings=pendings)
        out = et.apply_market_anchor_for_zoneless_signal(tpl, 4398.96, 4398.96)
        assert out["anchors"] + out["pendings"] == pendings


def test_the_tuned_geometry_is_left_alone():
    """Only the leg split changes -- SL, lots and the TP ladder are what the
    backtest measured and must survive untouched."""
    tpl = _tpl()
    out = et.apply_market_anchor_for_zoneless_signal(tpl, 4398.96, 4398.96)
    for key in ("sl_pips", "lot_anchor", "lot_pending",
                "tp1_pips", "tp2_pips", "tp3_pips", "mode"):
        assert out[key] == tpl[key], f"{key} was modified"


def test_the_stored_template_is_not_mutated():
    """The same template serves ranged signals from other channels; mutating
    it in place would convert their grids too, permanently and invisibly."""
    tpl = _tpl()
    et.apply_market_anchor_for_zoneless_signal(tpl, 4398.96, 4398.96)
    assert tpl["anchors"] == 0
    assert tpl["pendings"] == 2


# ── what it must NOT touch ───────────────────────────────────────────────────

def test_a_ranged_signal_keeps_its_full_resting_grid():
    """The whole point of a limit template: price retraces into the stated
    zone and fills at a better price. That must be unaffected."""
    tpl = _tpl()
    out = et.apply_market_anchor_for_zoneless_signal(tpl, 4395.0, 4400.0)
    assert out is tpl
    assert out["anchors"] == 0 and out["pendings"] == 2


def test_a_template_that_already_takes_an_anchor_is_untouched():
    tpl = _tpl(anchors=1, pendings=1)
    out = et.apply_market_anchor_for_zoneless_signal(tpl, 4398.96, 4398.96)
    assert out is tpl


def test_non_grid_templates_are_untouched():
    """A single-mode template already opens at market; it has no resting legs
    to convert."""
    tpl = _tpl(mode="single", anchors=0, pendings=2)
    out = et.apply_market_anchor_for_zoneless_signal(tpl, 4398.96, 4398.96)
    assert out is tpl


def test_a_template_with_no_pendings_is_untouched():
    """Nothing to convert. Promoting a leg here would invent exposure the
    template does not define."""
    tpl = _tpl(anchors=0, pendings=0)
    out = et.apply_market_anchor_for_zoneless_signal(tpl, 4398.96, 4398.96)
    assert out is tpl
    assert out["anchors"] == 0


def test_missing_template_is_handled():
    assert et.apply_market_anchor_for_zoneless_signal(None, 1.0, 1.0) is None
    assert et.apply_market_anchor_for_zoneless_signal({}, 1.0, 1.0) == {}


# ── wiring ───────────────────────────────────────────────────────────────────

def test_open_trade_applies_the_conversion_on_the_ea_copy():
    """Guards the call site: the conversion has to run on the template that
    reaches the EA, since anchors/pendings are what HandleOpenTemplateGrid
    stages from. core_signal_resolution re-reads the template from the DB, so
    adjusting it there would not propagate."""
    import inspect
    from backend.src.services.trading import open_trade
    src = inspect.getsource(core_open_trade)
    assert "apply_market_anchor_for_zoneless_signal(" in src
