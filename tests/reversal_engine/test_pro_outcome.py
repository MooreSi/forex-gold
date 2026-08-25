"""Decision table for pro_outcome._resolve_row -- did the professionals' call
actually work?

The expected behaviour asserted here comes from the rules the feature was
specified with, not from reading the implementation back:

  * A call is FILLED when a candle's range overlaps their stated entry zone,
    priced at the edge the market would have filled them at (the top of the
    zone for a BUY, the bottom for a SELL) -- never the mid, which would
    hand a wide zone a better entry than the market gave.
  * Filled, then TP1 before stop  -> win, R = reward/risk measured from the fill.
  * Filled, then stop before TP1  -> loss, R = -1.
  * Both levels inside ONE M1 bar -> loss. M1 OHLC cannot say which came
    first, and an ambiguous bar must not flatter them.
  * Zone never reached inside the fill window -> no_fill (NOT a loss: they
    never took the trade, so it is not evidence about their judgement).
  * Still open at the hold limit -> timeout, scored at that bar's close.
  * Nothing decided yet -> None, so the caller keeps walking next pass.
"""
import pytest

from backend.src.services.reversal_engine import pro_outcome


T0 = 1_785_000_000.0


def candle(offset_min, high, low, close=None):
    return {"time": T0 + offset_min * 60, "high": high, "low": low,
            "close": close if close is not None else (high + low) / 2}


def buy_row(**over):
    row = {"id": 1, "direction": "BUY", "signal_ts": T0,
           "entry_low": 4100.0, "entry_high": 4102.0,
           "stop_loss": 4090.0, "tp1": 4120.0,
           "resolve_cursor_ts": None, "entry_fill_price": None}
    row.update(over)
    return row


def sell_row(**over):
    row = {"id": 2, "direction": "SELL", "signal_ts": T0,
           "entry_low": 4100.0, "entry_high": 4102.0,
           "stop_loss": 4112.0, "tp1": 4080.0,
           "resolve_cursor_ts": None, "entry_fill_price": None}
    row.update(over)
    return row


# ── Fill ──────────────────────────────────────────────────────────────────────

def test_buy_fills_at_the_top_of_the_stated_zone():
    # Price trades through the zone, then runs to TP1 on a later bar.
    candles = [candle(1, 4103.0, 4099.0), candle(2, 4121.0, 4110.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    assert verdict["fill"] == 4102.0


def test_sell_fills_at_the_bottom_of_the_stated_zone():
    candles = [candle(1, 4103.0, 4099.0), candle(2, 4090.0, 4079.0)]

    verdict = pro_outcome._resolve_row(sell_row(), candles)

    assert verdict["fill"] == 4100.0


def test_a_bar_that_misses_the_zone_entirely_leaves_it_unresolved():
    candles = [candle(1, 4098.0, 4095.0), candle(2, 4099.0, 4096.0)]

    assert pro_outcome._resolve_row(buy_row(), candles) is None


def test_zone_never_reached_within_the_fill_window_is_no_fill_not_a_loss():
    beyond = pro_outcome._FILL_WINDOW_S / 60 + 1
    candles = [candle(1, 4098.0, 4095.0), candle(beyond, 4098.0, 4095.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    assert verdict["outcome"] == "no_fill"
    assert verdict["r"] is None


# ── Win / loss ────────────────────────────────────────────────────────────────

def test_buy_that_reaches_tp1_before_its_stop_is_a_win_scored_from_the_fill():
    candles = [candle(1, 4103.0, 4099.0), candle(2, 4121.0, 4110.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    # fill 4102, stop 4090 -> risk 12; TP1 4120 -> reward 18.
    assert verdict["outcome"] == "win"
    assert verdict["r"] == pytest.approx(1.5)


def test_buy_that_hits_its_stop_first_is_a_loss_at_minus_one_r():
    candles = [candle(1, 4103.0, 4099.0), candle(2, 4101.0, 4089.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    assert verdict["outcome"] == "loss"
    assert verdict["r"] == -1.0


def test_sell_that_reaches_tp1_before_its_stop_is_a_win():
    candles = [candle(1, 4103.0, 4099.0), candle(2, 4095.0, 4079.0)]

    verdict = pro_outcome._resolve_row(sell_row(), candles)

    # fill 4100, stop 4112 -> risk 12; TP1 4080 -> reward 20.
    assert verdict["outcome"] == "win"
    assert verdict["r"] == pytest.approx(20 / 12, rel=1e-3)


def test_sell_that_hits_its_stop_first_is_a_loss():
    candles = [candle(1, 4103.0, 4099.0), candle(2, 4113.0, 4101.0)]

    verdict = pro_outcome._resolve_row(sell_row(), candles)

    assert verdict["outcome"] == "loss"


def test_one_bar_touching_both_stop_and_tp1_is_scored_a_loss():
    # A single M1 bar spanning both levels: the order is unknowable, so the
    # conservative reading must win. If this ever returns "win", the corpus
    # starts flattering them on exactly the violent bars that matter most.
    candles = [candle(1, 4103.0, 4099.0), candle(2, 4125.0, 4085.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    assert verdict["outcome"] == "loss"


def test_fill_and_stop_inside_the_same_bar_still_resolves():
    # The entry candle itself blows through the stop -- it must not be
    # ignored just because that bar was also the fill.
    candles = [candle(1, 4103.0, 4085.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    assert verdict["outcome"] == "loss"


# ── Timeout ───────────────────────────────────────────────────────────────────

def test_still_open_at_the_hold_limit_is_scored_at_that_bars_close():
    beyond = pro_outcome._MAX_HOLD_S / 60 + 1
    candles = [candle(1, 4103.0, 4099.0),
               candle(beyond, 4108.0, 4104.0, close=4108.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    # fill 4102, close 4108 -> +6 on 12 of risk.
    assert verdict["outcome"] == "timeout"
    assert verdict["r"] == pytest.approx(0.5)


def test_a_drifting_trade_cannot_masquerade_as_a_win():
    beyond = pro_outcome._MAX_HOLD_S / 60 + 1
    candles = [candle(1, 4103.0, 4099.0),
               candle(beyond, 4103.0, 4097.0, close=4098.0)]

    verdict = pro_outcome._resolve_row(buy_row(), candles)

    assert verdict["outcome"] == "timeout"
    assert verdict["r"] < 0


# ── Resuming from a previous pass ─────────────────────────────────────────────

def test_a_row_already_filled_last_pass_is_not_refilled_at_a_new_price():
    # entry_fill_price carries across passes; a later bar re-entering the
    # zone must not silently re-price the entry.
    candles = [candle(10, 4121.0, 4101.0)]

    verdict = pro_outcome._resolve_row(
        buy_row(entry_fill_price=4101.0), candles)

    assert verdict["outcome"] == "win"
    assert verdict["fill"] == 4101.0
    # risk from the CARRIED fill (4101 - 4090 = 11), not from the zone top.
    assert verdict["r"] == pytest.approx(19 / 11, rel=1e-3)


def test_a_zero_risk_call_is_rejected_rather_than_scored():
    candles = [candle(1, 4103.0, 4099.0)]

    verdict = pro_outcome._resolve_row(
        buy_row(stop_loss=4101.0, entry_low=4100.0, entry_high=4102.0), candles)

    assert verdict["outcome"] == "invalid"
