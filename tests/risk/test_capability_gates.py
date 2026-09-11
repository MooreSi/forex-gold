"""Reading the new capability switches, and defaulting to today.

Every capability from docs/todo/reversal-engine/200 ships behind its own
risk setting, all of them off. This module is the single place those
settings are read and turned into the config objects the pure modules take,
so there is one answer to "is this on" rather than one per call site.

The test that matters most is the first one: with a settings row straight
out of migration 41, every gate is inert.
"""
from __future__ import annotations

import pytest

from backend.src.services.risk import capability_gates as cg


DEFAULTS = {
    "re_atr_barriers_enabled": 0, "re_atr_stop_mult": 1.2,
    "re_atr_tp1_mult": 1.2,
    "entry_trigger_enabled": 0, "entry_trigger_rejection": 0,
    "entry_trigger_deceleration": 0, "entry_trigger_max_range_ratio": 0.5,
    "meta_label_gate_enabled": 0, "meta_label_threshold": 0.5,
    "session_liquidity_gate_enabled": 0, "event_tier_gate_enabled": 0,
    "vol_target_sizing_enabled": 0, "correlated_exposure_cap_lots": 0.0,
    "liquidity_map_levels_enabled": 0,
}


class TestEverythingOffByDefault:
    def test_atr_barriers_are_off(self):
        assert cg.atr_barrier_config(DEFAULTS) is None

    def test_the_entry_trigger_is_off(self):
        assert cg.entry_trigger_config(DEFAULTS) is None

    def test_the_meta_label_gate_is_off(self):
        assert cg.meta_label_gate(DEFAULTS) == (False, 0.5)

    def test_the_liquidity_gates_allow_everything(self):
        assert cg.liquidity_blocks(1_788_998_400.0, DEFAULTS, events=[]) is None

    def test_the_new_levels_are_not_added(self):
        assert cg.liquidity_map_enabled(DEFAULTS) is False

    def test_sizing_is_unchanged(self):
        out = cg.sizing_inputs(DEFAULTS, atr=16.0, reference_atr=8.0,
                               drawdown_pct=0.5, open_correlated_lots=9.0)
        from backend.src.services.risk import sizing_policy as sp
        assert sp.apply(0.10, out).lots == pytest.approx(0.10)

    def test_a_settings_row_missing_the_columns_entirely_is_also_inert(self):
        """A client that has not run migration 41 yet must behave exactly as
        it did, not crash and not silently enable something."""
        assert cg.atr_barrier_config({}) is None
        assert cg.entry_trigger_config({}) is None
        assert cg.liquidity_blocks(1_788_998_400.0, {}, events=[]) is None


class TestTurningThemOn:
    def _on(self, **kw):
        rs = dict(DEFAULTS)
        rs.update(kw)
        return rs

    def test_atr_barriers_come_through_with_their_multiples(self):
        cfg = cg.atr_barrier_config(self._on(re_atr_barriers_enabled=1,
                                             re_atr_stop_mult=1.5,
                                             re_atr_tp1_mult=1.4))
        assert cfg == {"enabled": True, "stop_mult": 1.5, "tp1_mult": 1.4}

    def test_the_entry_trigger_carries_only_the_checks_asked_for(self):
        cfg = cg.entry_trigger_config(self._on(entry_trigger_enabled=1,
                                               entry_trigger_rejection=1))
        assert cfg.require_rejection is True
        assert cfg.require_deceleration is False

    def test_an_enabled_trigger_with_no_checks_selected_is_still_inert(self):
        """Enabling the gate and choosing nothing must not mean "block
        everything" and must not mean "silently on". It means no check is
        required, which is what the switches say."""
        cfg = cg.entry_trigger_config(self._on(entry_trigger_enabled=1))
        assert cfg is not None
        from backend.src.services.market import entry_trigger as et
        assert et.confirm([], 3300.0, "BUY", 8.0, cfg).passed is True

    def test_the_rollover_window_blocks_when_the_gate_is_on(self):
        from datetime import datetime, timezone
        ts = datetime(2026, 9, 9, 22, 0, tzinfo=timezone.utc).timestamp()
        assert cg.liquidity_blocks(ts, self._on(session_liquidity_gate_enabled=1),
                                   events=[]) is not None

    def test_an_imminent_tier_one_event_blocks_when_that_gate_is_on(self):
        events = [{"title": "FOMC Statement", "impact": "high",
                   "currency": "USD", "mins_until": 15.0}]
        reason = cg.liquidity_blocks(1_788_998_400.0,
                                     self._on(event_tier_gate_enabled=1),
                                     events=events)
        assert reason and "FOMC" in reason

    def test_the_correlated_cap_reaches_the_sizing_policy(self):
        out = cg.sizing_inputs(self._on(correlated_exposure_cap_lots=0.10),
                               atr=0.0, reference_atr=0.0, drawdown_pct=0.0,
                               open_correlated_lots=0.07)
        from backend.src.services.risk import sizing_policy as sp
        assert sp.apply(0.10, out).lots == pytest.approx(0.03)

    def test_volatility_scaling_only_applies_when_its_own_switch_is_on(self):
        from backend.src.services.risk import sizing_policy as sp
        off = cg.sizing_inputs(self._on(), atr=16.0, reference_atr=8.0,
                               drawdown_pct=0.0, open_correlated_lots=0.0)
        on = cg.sizing_inputs(self._on(vol_target_sizing_enabled=1), atr=16.0,
                              reference_atr=8.0, drawdown_pct=0.0,
                              open_correlated_lots=0.0)
        assert sp.apply(0.10, off).lots == pytest.approx(0.10)
        assert sp.apply(0.10, on).lots < 0.10
