"""Fixed R:R (STRATEGY_FIXED_RR) -- one broker stop, one broker target, no
breakeven move, lot recomputed from the fixed stop.

The design is derived from measured trade paths (tools/exit_policy_lab.py),
so these tests lock in the properties that make it work rather than just
the plumbing: risk normalisation, a real broker TP, no breakeven, and --
critically -- that it never falls through to scale_out's dispatch, which
partial-closes against tp levels this strategy does not use.
"""
import pytest

from backend.src.services.risk import strategy_params as sp
from backend.src.services.broker.ea_bridge import EA_PORTABLE_STRATEGIES
from backend.src.services.trading.fees_sizing import CONTRACT_SIZE
from backend.src.utils.models import (
    STRATEGY_FIXED_RR, STRATEGY_NAMES, STRATEGY_DESCRIPTIONS,
)


def test_registered_with_name_and_description():
    assert STRATEGY_NAMES[STRATEGY_FIXED_RR] == "Fixed R:R"
    assert STRATEGY_FIXED_RR in STRATEGY_DESCRIPTIONS
    assert STRATEGY_FIXED_RR in sp.PARAM_STRATEGIES


def test_default_params_match_the_validated_sweep_winner():
    """s4/t6 is the configuration the lab's holdout + bootstrap supported;
    if these defaults drift, re-run tools/exit_policy_lab.py first."""
    specs = dict((k, d) for k, _l, d, _u in sp.PARAM_SPECS[STRATEGY_FIXED_RR])
    assert specs["sl_pt"] == 4.0
    assert specs["tp_pt"] == 6.0


def test_target_is_wider_than_stop():
    """The whole point -- the previous geometry had a 7.89pt stop against a
    4.29pt average best-case move, capping even a perfect exit near 0.54R."""
    specs = dict((k, d) for k, _l, d, _u in sp.PARAM_SPECS[STRATEGY_FIXED_RR])
    assert specs["tp_pt"] > specs["sl_pt"]


def test_is_ea_portable():
    assert STRATEGY_FIXED_RR in EA_PORTABLE_STRATEGIES


def test_no_breakeven_parameter_exists():
    """Breakeven moves measured -0.10 to -0.36R in every configuration
    tested (8/8, both halves). There must be no knob that reintroduces
    one by accident."""
    keys = {k for k, _l, _d, _u in sp.PARAM_SPECS[STRATEGY_FIXED_RR]}
    assert not any("be" in k or "breakeven" in k for k in keys), keys


@pytest.mark.parametrize("balance,risk_pct,sl_pt", [
    (920.0, 0.5, 4.0),
    (5000.0, 1.0, 4.0),
    (20000.0, 0.5, 4.0),
])
def test_lot_from_fixed_stop_normalises_risk(balance, risk_pct, sl_pt, monkeypatch):
    """Risk per trade must be ~constant -- the failure that made a fixed
    0.1 lot risk $4.87 on one trade and $300 on another the same day."""
    from backend.src.services.trading import fees_sizing as fs
    monkeypatch.setattr(fs.db_module, "get_risk_settings",
                        lambda: {"max_lot_size": 10.0, "max_risk_per_trade_pct": 0})
    entry = 4000.0
    lot = fs.suggest_lot_size(entry, entry - sl_pt, balance, risk_pct)
    realised_risk = sl_pt * lot * CONTRACT_SIZE
    intended = balance * risk_pct / 100
    assert abs(realised_risk - intended) / intended < 0.15


def test_min_lot_floor_forces_overrisk_on_wide_stops_at_small_balance(monkeypatch):
    """Documents a real limit, not a bug in this strategy: the broker's
    0.01 minimum lot sets a floor on risk. At a ~$920 balance targeting
    0.5% ($4.60), a 4pt stop lands within ~13% of intent, but an 8pt stop
    cannot go below $8 -- 74% over. Sizing can only normalise risk while
    the intended lot stays above the minimum, which is an additional
    argument for this strategy's tighter stop on a small account."""
    from backend.src.services.trading import fees_sizing as fs
    monkeypatch.setattr(fs.db_module, "get_risk_settings",
                        lambda: {"max_lot_size": 10.0, "max_risk_per_trade_pct": 0})
    entry, balance, risk_pct = 4000.0, 920.0, 0.5
    intended = balance * risk_pct / 100

    lot4 = fs.suggest_lot_size(entry, entry - 4.0, balance, risk_pct)
    risk4 = 4.0 * lot4 * CONTRACT_SIZE
    assert abs(risk4 - intended) / intended < 0.15

    lot8 = fs.suggest_lot_size(entry, entry - 8.0, balance, risk_pct)
    risk8 = 8.0 * lot8 * CONTRACT_SIZE
    assert lot8 == 0.01, "clamped to the broker minimum"
    assert risk8 > intended * 1.5, "wide stop cannot reach the risk target here"


def test_broker_tp_is_set_for_fixed_rr():
    """MT5 must hold the target itself -- nothing polls this strategy."""
    import inspect
    from backend.src.services.trading import open_trade as core_open_trade
    src = inspect.getsource(core_open_trade.open_trade)
    assert "STRATEGY_FIXED_RR" in src
    assert "mt5_tp = tp1" in src


def test_engine_dispatch_has_explicit_branch_not_scale_out_fallthrough():
    """Regression guard for the bug class that fabricated $40,730 of PnL:
    an unrecognised strategy falls through to _handle_scale_out, which
    partial-closes against tp1/tp2/tp3 using whatever entry_price the row
    holds."""
    import inspect
    # The strategy dispatch moved off the engine into the monitor cycle
    # during the refactor; the guard follows it. (2026-08-25 merge.)
    from backend.src.services.positions import monitor_cycle as engine
    src = inspect.getsource(engine)
    assert "elif strategy == STRATEGY_FIXED_RR:" in src


def test_ea_dispatch_has_explicit_branch():
    """Same guard on the MQL5 side -- its dispatch also defaults to
    ManageScaleOut."""
    from pathlib import Path
    mq5 = Path(__file__).resolve().parents[2] / "mql5" / "ForexTraderBridge.mq5"
    src = mq5.read_text()
    assert 'else if(t.strategy == "fixed_rr")' in src
    # and it must get a genuine broker TP
    assert 'strategy == "fixed_rr"' in src.split("double brokerTp")[1][:900]
