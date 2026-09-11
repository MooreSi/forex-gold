"""DPM's breakeven trigger is a threshold in ACCOUNT dollars, and must be
derived as one.

`compute_adaptive_params` builds it from ATR, which `compute_atr` documents
as "price value ($)" -- dollars per ounce of gold. The handler then compares
it against `pnl(...)`, which is `(current - entry) * lots * CONTRACT_SIZE`
-- dollars in the account. Those two are the same number only when
`lots * 100 == 1`, i.e. at 0.01 lots. At every other size the threshold is
wrong by a factor of `lots * 100`: at the 0.1 lots this account actually
trades, breakeven arms after a tenth of the intended price movement.

The calibration half of the same module already does the conversion, and
its comment claims the runtime agrees with it:

    # Units: atr ($/oz) * lot (lots) * 100 (oz/lot) = USD
    # ... matching compute_adaptive_params behaviour.
    hypothetical_be = be_m * atr * sm * lot * 100.0

These tests make that claim true. The intended behaviour, stated
independently of the implementation: **the BE threshold corresponds to a
fixed distance in gold price -- roughly 0.6 x ATR after the regime and
session factors -- whatever lot size the trade happens to carry.**

The safety clamp stays in account dollars, where `[2.0, 30.0]` is a
sensible band, and is asserted here so that a later change to the formula
cannot quietly remove it.

Nothing in this file touches a broker: these are pure computations over
constructed candle data.
"""
import pytest

from backend.src.services.dpm import engine as dpm_engine


# Thirty identical M5 bars, each with a true range of exactly $4.00
# (high-low = 4, and the previous close sits inside the bar, so no gap term
# can win). Wilder's ATR over a constant series is that constant, so
# compute_atr returns 4.00 and the arithmetic below is checkable by hand.
_CANDLES = [{"high": 2404.0, "low": 2400.0, "close": 2402.0,
             "open": 2402.0} for _ in range(30)]

# London (session multiplier 1.20) and a ranging regime (be_mult 0.90), both
# pinned so the expected value does not move with the wall clock or with a
# regime detector reading flat candles.
_SESSION_MULT = 1.20
_REGIME_BE_MULT = 0.90
_ATR = 4.00

# What compute_adaptive_params applies on top of the regime figure.
_BASE_BE_FACTOR = 0.6

# The price distance the trade must travel in gold dollars before breakeven
# arms: 4.00 x (0.6 x 0.90) x 1.20 = $2.592/oz. This is the number that must
# NOT change with lot size.
_EXPECTED_PRICE_DISTANCE = _ATR * (_BASE_BE_FACTOR * _REGIME_BE_MULT) * _SESSION_MULT


@pytest.fixture(autouse=True)
def _pinned_market(monkeypatch):
    monkeypatch.setattr(dpm_engine, "detect_session", lambda: "london")
    monkeypatch.setattr(dpm_engine, "detect_regime", lambda candles, atr: "ranging")


class _Tick:
    bid = 2402.0
    ask = 2402.5


def _trade(lots: float) -> dict:
    return {
        "trade_id": "t-1",
        "direction": "BUY",
        "entry_price": 2400.0,
        "lot_size": lots,
        "remaining_lots": lots,
        "stop_loss": 2390.0,
    }


def _be_trigger(lots: float, candles=None) -> float:
    params = dpm_engine.compute_adaptive_params(
        _trade(lots), _Tick(), candles if candles is not None else _CANDLES,
    )
    return params["be_trigger_usd"]


def test_the_threshold_is_the_account_value_of_the_intended_price_move():
    """0.1 lots on gold is $10 of P&L per $1 of price. A $2.592 move is
    therefore $25.92 in the account, and that is what the handler must be
    given to compare against unrealised P&L."""
    assert _be_trigger(0.1) == pytest.approx(
        _EXPECTED_PRICE_DISTANCE * 0.1 * 100.0, abs=0.1)


def test_the_same_price_move_at_a_tenth_of_the_size():
    """0.01 lots: $1 of P&L per $1 of price, so the same $2.592 move is
    $2.59. This is the one size at which the old formula was accidentally
    right, which is why it went unnoticed."""
    assert _be_trigger(0.01) == pytest.approx(
        _EXPECTED_PRICE_DISTANCE * 0.01 * 100.0, abs=0.1)


def test_the_threshold_scales_with_lot_size():
    """The relationship the old formula lacked entirely: ten times the size
    is ten times the account value for the same market move. ATR is chosen
    so neither the floor nor the cap binds at either size."""
    assert _be_trigger(0.1) == pytest.approx(_be_trigger(0.01) * 10.0, abs=0.2)


def test_the_price_distance_is_the_same_at_every_size():
    """Stated the way it matters: convert each threshold back into gold
    dollars and both trades wait for the same move before locking in."""
    small = _be_trigger(0.01) / (0.01 * 100.0)
    large = _be_trigger(0.1) / (0.1 * 100.0)
    assert small == pytest.approx(_EXPECTED_PRICE_DISTANCE, abs=0.02)
    assert large == pytest.approx(_EXPECTED_PRICE_DISTANCE, abs=0.02)


def test_it_matches_the_model_calibration_optimises_against():
    """run_calibration picks be_multiplier by simulating
    `be_m * atr * sm * lot * 100` against recorded peak P&L, then feeds the
    winner back in as this function's own base multiplier. If the two
    formulas disagree, the calibrated value is optimal for a threshold that
    is not the one in force."""
    lot = 0.1
    calibration_model = (
        (_BASE_BE_FACTOR * _REGIME_BE_MULT) * _ATR * _SESSION_MULT * lot * 100.0
    )
    assert _be_trigger(lot) == pytest.approx(calibration_model, abs=0.1)


def test_the_floor_still_applies_in_account_dollars():
    """A near-flat market must not produce a threshold of pennies. Bars with
    a $0.10 range put the raw figure at $0.06 on 0.01 lots; the floor lifts
    it to $2.00."""
    quiet = [{"high": 2400.1, "low": 2400.0, "close": 2400.05,
              "open": 2400.05} for _ in range(30)]
    assert _be_trigger(0.01, quiet) == pytest.approx(dpm_engine._MIN_BE)


def test_the_cap_still_applies_in_account_dollars():
    """A violent market on a large position must not defer breakeven
    indefinitely: $40 bars at 0.1 lots compute to $259 and are capped at
    $30. Note what the cap means once the units are right -- on 0.1 lots it
    binds above an ATR of roughly $4.6, so in a fast session the threshold
    is the cap rather than the ATR formula."""
    violent = [{"high": 2440.0, "low": 2400.0, "close": 2420.0,
                "open": 2420.0} for _ in range(30)]
    assert _be_trigger(0.1, violent) == pytest.approx(dpm_engine._MAX_BE)


def test_a_trade_with_no_lot_size_falls_back_to_the_minimum_size():
    """Defensive: the handler always supplies lots, but a malformed row must
    not collapse the threshold to zero and arm breakeven on the first tick.
    0.01 is the smallest tradeable size and the old formula's implied one."""
    trade = _trade(0.1)
    trade.pop("lot_size")
    trade.pop("remaining_lots")
    params = dpm_engine.compute_adaptive_params(trade, _Tick(), _CANDLES)
    assert params["be_trigger_usd"] == pytest.approx(
        _EXPECTED_PRICE_DISTANCE * 0.01 * 100.0, abs=0.1)
