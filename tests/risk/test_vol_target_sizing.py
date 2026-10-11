"""Volatility-target sizing, wired, and inert while its switch is off.

docs/todo/016 phase 2, item 5. `vol_target_sizing_enabled` existed on the
Reversal Engine card from 2026-09-11 with nothing behind it. It now scales a
Reversal Engine lot by `sizing_policy.apply`: smaller when ATR is above its
five-day median, larger (capped at 1.5x and by Max lot size) when below, and
smaller in drawdown. The rules pinned here:

  * switch OFF: the lot is returned untouched and the policy is never called;
  * only the Reversal Engine's own signals are scaled (it is that engine's
    card), never a Telegram channel or another engine;
  * a deliberate fixed lot (a typed override, a lot on the signal, a fixed
    template anchor, Fixed lots mode) is never scaled, the same exemption the
    channel multiplier has;
  * the correlated-exposure cap is NOT applied: nothing counts open
    correlated lots yet, so it stays unwired rather than half-wired.

Fakes and a temp database. Nothing here places, closes or modifies an order,
real or demo.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from backend.src.db import database as db
from backend.src.services.risk import sizing_policy
from backend.src.services.risk import vol_target as vt
from backend.src.services.signals import resolution as sr
from tests._fakes import _ReconciliationBridge

RE = "Reversal Engine"


def _market(monkeypatch, atr, ref, drawdown=0.0):
    from backend.src.services.reversal_engine import vol_regime as vr
    monkeypatch.setattr(vr, "latest_atr", lambda now=None: atr)
    monkeypatch.setattr(vr, "reference_atr_now", lambda now=None: ref)
    monkeypatch.setattr(vt, "_drawdown_pct", lambda: drawdown)


def _scale(lot, rs, source=RE, fixed=False):
    return asyncio.run(vt.scale_lot(lot, rs, source=source, fixed=fixed))


ON = {"vol_target_sizing_enabled": 1}


class TestOffIsUntouched:
    def test_the_lot_comes_back_as_it_went_in(self, monkeypatch):
        _market(monkeypatch, atr=20.0, ref=5.0, drawdown=0.5)
        assert _scale(0.13, {}) == 0.13

    def test_the_policy_is_not_even_consulted(self, monkeypatch):
        _market(monkeypatch, atr=20.0, ref=5.0)

        def boom(*a, **k):
            raise AssertionError("sizing_policy.apply called with the switch off")
        monkeypatch.setattr(sizing_policy, "apply", boom)
        assert _scale(0.13, {"vol_target_sizing_enabled": 0}) == 0.13


class TestOnScalesTheReversalEngine:
    def test_twice_normal_volatility_halves_the_lot(self, monkeypatch):
        _market(monkeypatch, atr=10.0, ref=5.0)
        assert _scale(0.10, ON) == 0.05

    def test_a_quiet_market_scales_up_but_never_past_max_lot(self, monkeypatch):
        _market(monkeypatch, atr=2.5, ref=5.0)
        assert _scale(0.10, dict(ON, max_lot_size=0.12)) == 0.12

    def test_normal_volatility_leaves_it_alone(self, monkeypatch):
        _market(monkeypatch, atr=5.0, ref=5.0)
        assert _scale(0.10, ON) == 0.10

    def test_no_volatility_reading_means_no_volatility_scaling(self, monkeypatch):
        _market(monkeypatch, atr=None, ref=None)
        assert _scale(0.10, ON) == 0.10

    def test_drawdown_scales_down_under_the_same_switch(self, monkeypatch):
        _market(monkeypatch, atr=5.0, ref=5.0, drawdown=0.20)
        assert _scale(0.20, ON) == 0.05


class TestWhatIsNeverScaled:
    def test_another_source(self, monkeypatch):
        _market(monkeypatch, atr=10.0, ref=5.0)
        assert _scale(0.10, ON, source="GOLD DIGGERS INSTITUTIONAL") == 0.10
        assert _scale(0.10, ON, source="Breakout Engine") == 0.10

    def test_a_fixed_lot(self, monkeypatch):
        _market(monkeypatch, atr=10.0, ref=5.0)
        assert _scale(0.10, ON, fixed=True) == 0.10

    def test_the_correlated_cap_is_not_applied(self, monkeypatch):
        _market(monkeypatch, atr=5.0, ref=5.0)
        assert _scale(0.10, dict(ON, correlated_exposure_cap_lots=0.02)) == 0.10


# ── Through the real resolution path ────────────────────────────────────────

def _bridge():
    # The shared read-surface fake rather than a 51st local _FakeBridge
    # (tests/refactor/test_fixture_dedup.py). 1% of 10,000 over a 10-point
    # stop is 0.10 lots: far enough above the 0.01 floor that halving shows.
    return _ReconciliationBridge(
        account={"balance": 10000.0},
        tick=SimpleNamespace(bid=2399.8, ask=2400.2, spread_points=4.0))


def _insert(source):
    with db.db() as conn:
        conn.execute(
            "INSERT INTO vantage_signals (signal_id, source_name, direction, entry_low, "
            "entry_high, stop_loss, tp1, status, created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            ("sig-1", source, "BUY", 2399.0, 2401.0, 2390.0, 2420.0, "pending", time.time()))


def _resolve():
    return asyncio.run(sr.resolve_open_trade_params(_bridge(), "sig-1"))["lot_size"]


def _baseline(fresh_db, source):
    db.update_risk_settings({"risk_per_trade_pct": 1.0, "max_lot_size": 1.0})
    _insert(source)
    base = _resolve()
    assert base >= 0.04, base   # the scaling below must be visible
    return base


class TestResolution:
    def test_off_the_reversal_engine_lot_is_unchanged(self, fresh_db, monkeypatch):
        _market(monkeypatch, atr=10.0, ref=5.0)
        base = _baseline(fresh_db, RE)
        db.update_risk_settings({"vol_target_sizing_enabled": 0})
        assert _resolve() == base

    def test_on_the_reversal_engine_lot_is_halved_at_twice_normal_atr(self, fresh_db, monkeypatch):
        _market(monkeypatch, atr=10.0, ref=5.0)
        base = _baseline(fresh_db, RE)
        db.update_risk_settings({"vol_target_sizing_enabled": 1})
        assert _resolve() == max(0.01, round(base * 0.5, 2))
        assert _resolve() < base

    def test_on_a_telegram_channel_is_untouched(self, fresh_db, monkeypatch):
        _market(monkeypatch, atr=10.0, ref=5.0)
        base = _baseline(fresh_db, "TestChannel")
        db.update_risk_settings({"vol_target_sizing_enabled": 1})
        assert _resolve() == base

    def test_on_a_fixed_global_lot_is_untouched(self, fresh_db, monkeypatch):
        _market(monkeypatch, atr=10.0, ref=5.0)
        db.update_risk_settings({"strategy_lot_size": 0.07})
        base = _baseline(fresh_db, RE)
        db.update_risk_settings({"vol_target_sizing_enabled": 1})
        assert _resolve() == base


class TestTheLimitOrderPathIsWiredToo:
    def test_the_reversal_limit_route_scales_its_template_lot(self):
        """The RE limit route re-sizes a template's lot after resolution, so
        a hook in resolution alone would miss it."""
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2]
               / "backend/src/services/reversal_engine/reversal_engine_live_execute.py"
               ).read_text(encoding="utf-8")
        assert "vol_target.scale_lot(" in src


class TestTheCardSaysWhatItDoes:
    """The card lost its "not yet connected" warning in the React port and
    then promised a feature that did nothing. Now that it does something,
    the card has to say what, to which trades, and where to try it."""

    def _card(self):
        from pathlib import Path
        src = (Path(__file__).resolve().parents[2]
               / "frontend/src/components/engines/content/capabilities.ts"
               ).read_text(encoding="utf-8")
        return src[src.index('"vol_target_sizing_enabled"'):][:1400]

    def test_it_names_the_reversal_engine(self):
        assert "Reversal Engine" in self._card()

    def test_it_says_fixed_lots_are_left_alone(self):
        assert "fixed" in self._card().lower()

    def test_it_says_to_try_it_on_demo(self):
        assert "demo" in self._card().lower()

    def test_it_carries_the_measurement_against_it(self):
        """Handover 040 (closed 2026-10-07): on this book volatility
        targeting would have lost $1,002 more than a flat size change. The
        owner kept the switch, wired and off, on condition that the card
        says so, so nobody turns it on believing it helps."""
        card = self._card()
        assert "040" in card
        assert "1,002" in card
