"""core_strategy_params.py: live-tunable per-strategy SL/TP values (an
app_config-backed override merged over built-in defaults) plus a named
template library (strategy_param_templates table). Added 2026-07-22 as
Trading > Strategy > Strategy Parameters, mirroring a third-party EA's
"Settings Templates" panel -- see that module's docstring for scope.
"""
import os
import tempfile

import pytest

from forex_trader.core import database as db
from forex_trader.core import core_strategy_params as sp
from forex_trader.core.models import (
    STRATEGY_CONSERVATIVE, STRATEGY_SCALP_RUNNER, STRATEGY_REVERSAL_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER, STRATEGY_ADAPTIVE_RUNNER_2,
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_SIGNAL_CLIMBER, STRATEGY_PROTECTED_SCALE,
    STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER,
)


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    sp._cache.clear()
    yield db
    sp._cache.clear()
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


# ── default_params / PARAM_SPECS shape ───────────────────────────────────────

def test_default_params_matches_param_specs(fresh_db):
    for strategy, specs in sp.PARAM_SPECS.items():
        defaults = sp.default_params(strategy)
        assert defaults == {key: default for key, _label, default, _unit in specs}


def test_default_params_returns_a_copy_not_shared_state(fresh_db):
    a = sp.default_params(STRATEGY_CONSERVATIVE)
    a["sl_pt"] = 999.0
    b = sp.default_params(STRATEGY_CONSERVATIVE)
    assert b["sl_pt"] != 999.0


def test_unknown_strategy_has_no_defaults(fresh_db):
    assert sp.default_params("not_a_real_strategy") == {}


# ── 2026-07-24: Conservative Trial/Signal Climber/Protected Scale/Scale Out/
# BE Runner added to Strategy Parameters ───────────────────────────────────

@pytest.mark.parametrize("strategy", [
    STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_SIGNAL_CLIMBER, STRATEGY_PROTECTED_SCALE,
    STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER,
])
def test_new_strategies_are_fully_wired(fresh_db, strategy):
    assert strategy in sp.PARAM_STRATEGIES
    assert strategy in sp.PARAM_SPECS
    assert strategy in sp.STRATEGY_LABELS
    assert sp.PARAM_SPECS[strategy]  # at least one tunable param each


def test_conservative_trial_defaults_match_original_hardcoded_constants(fresh_db):
    defaults = sp.default_params(STRATEGY_CONSERVATIVE_TRIAL)
    assert defaults["sl_risk_usd"] == 100.0
    assert [defaults[f"tp{i}_pt"] for i in range(1, 7)] == [5.0, 10.0, 14.0, 20.0, 27.0, 35.0]
    assert [defaults[f"tp{i}_pct"] for i in range(1, 6)] == [5.0, 30.0, 20.0, 40.0, 5.0]


def test_scale_out_defaults_match_original_hardcoded_constants(fresh_db):
    defaults = sp.default_params(STRATEGY_SCALE_OUT)
    assert [defaults[f"tp{i}_pct"] for i in range(1, 5)] == [40.0, 30.0, 20.0, 10.0]


def test_protected_scale_default_matches_original_hardcoded_constant(fresh_db):
    assert sp.default_params(STRATEGY_PROTECTED_SCALE)["mid_tp_close_pct"] == 20.0


def test_be_runner_default_matches_original_hardcoded_constant(fresh_db):
    assert sp.default_params(STRATEGY_BE_RUNNER)["adx_ranging_threshold"] == 25.0


def test_signal_climber_default_be_at_pos_matches_original_hardcoded_value(fresh_db):
    # Original hardcoded call was be_at_pos=0 (0-indexed) == TP1 in the
    # 1-based "TP#" convention this param uses.
    assert sp.default_params(STRATEGY_SIGNAL_CLIMBER)["be_at_pos"] == 1.0


# ── get/set/reset live values ────────────────────────────────────────────────

def test_get_strategy_params_returns_defaults_with_no_override(fresh_db):
    assert sp.get_strategy_params(STRATEGY_CONSERVATIVE) == sp.default_params(STRATEGY_CONSERVATIVE)


def test_set_strategy_params_persists_and_is_read_back_immediately(fresh_db):
    sp.set_strategy_params(STRATEGY_CONSERVATIVE, {"sl_pt": 7.5, "tp1_pt": 4.0})
    result = sp.get_strategy_params(STRATEGY_CONSERVATIVE)
    assert result == {"sl_pt": 7.5, "tp1_pt": 4.0}


def test_set_strategy_params_drops_unknown_keys(fresh_db):
    sp.set_strategy_params(STRATEGY_CONSERVATIVE, {"sl_pt": 6.0, "not_a_real_param": 123.0})
    result = sp.get_strategy_params(STRATEGY_CONSERVATIVE)
    assert "not_a_real_param" not in result
    assert result["sl_pt"] == 6.0


def test_set_strategy_params_unknown_strategy_raises(fresh_db):
    with pytest.raises(ValueError):
        sp.set_strategy_params("not_a_real_strategy", {"sl_pt": 5.0})


def test_set_strategy_params_partial_update_keeps_other_fields_at_default(fresh_db):
    # Scalp Runner has sl_pt/tp1_pt/tp2_pt -- only override sl_pt.
    sp.set_strategy_params(STRATEGY_SCALP_RUNNER, {"sl_pt": 12.0})
    result = sp.get_strategy_params(STRATEGY_SCALP_RUNNER)
    defaults = sp.default_params(STRATEGY_SCALP_RUNNER)
    assert result["sl_pt"] == 12.0
    assert result["tp1_pt"] == defaults["tp1_pt"]
    assert result["tp2_pt"] == defaults["tp2_pt"]


def test_reset_strategy_params_reverts_to_defaults(fresh_db):
    sp.set_strategy_params(STRATEGY_REVERSAL_RUNNER, {"sl_mult": 9.0})
    assert sp.get_strategy_params(STRATEGY_REVERSAL_RUNNER)["sl_mult"] == 9.0
    sp.reset_strategy_params(STRATEGY_REVERSAL_RUNNER)
    assert sp.get_strategy_params(STRATEGY_REVERSAL_RUNNER) == sp.default_params(STRATEGY_REVERSAL_RUNNER)


def test_cache_is_invalidated_on_set_not_stale_for_ttl(fresh_db):
    # First read populates the cache with defaults.
    sp.get_strategy_params(STRATEGY_ADAPTIVE_RUNNER_2)
    sp.set_strategy_params(STRATEGY_ADAPTIVE_RUNNER_2, {"sl_pt": 15.0})
    # Must reflect the new value immediately, not the cached default for
    # up to _CACHE_TTL seconds.
    assert sp.get_strategy_params(STRATEGY_ADAPTIVE_RUNNER_2)["sl_pt"] == 15.0


def test_missing_db_override_field_falls_back_to_default(fresh_db):
    """A saved override that predates a newly-added param field must not
    make that field silently disappear from the returned dict."""
    sp.set_strategy_params(STRATEGY_ADAPTIVE_RUNNER, {"sl_mult": 3.0})
    # Simulate a stale override missing tp_cap_frac by writing raw JSON
    # directly, bypassing set_strategy_params()'s own defaulting.
    import json
    db.set_app_config(f"strategy_params_{STRATEGY_ADAPTIVE_RUNNER}", json.dumps({"sl_mult": 3.0}))
    sp._cache.clear()
    result = sp.get_strategy_params(STRATEGY_ADAPTIVE_RUNNER)
    assert result["sl_mult"] == 3.0
    assert result["tp_cap_frac"] == sp.default_params(STRATEGY_ADAPTIVE_RUNNER)["tp_cap_frac"]


# ── template library ──────────────────────────────────────────────────────────

def test_save_and_list_templates(fresh_db):
    tid = sp.save_template(STRATEGY_CONSERVATIVE, "Tight Scalp", {"sl_pt": 3.0, "tp1_pt": 2.0})
    assert tid
    templates = sp.list_templates(STRATEGY_CONSERVATIVE)
    assert len(templates) == 1
    assert templates[0]["name"] == "Tight Scalp"
    assert templates[0]["params"] == {"sl_pt": 3.0, "tp1_pt": 2.0}


def test_list_templates_scoped_to_strategy(fresh_db):
    sp.save_template(STRATEGY_CONSERVATIVE, "A", {"sl_pt": 3.0})
    sp.save_template(STRATEGY_SCALP_RUNNER, "B", {"sl_pt": 8.0})
    assert len(sp.list_templates(STRATEGY_CONSERVATIVE)) == 1
    assert len(sp.list_templates(STRATEGY_SCALP_RUNNER)) == 1


def test_save_template_requires_a_name(fresh_db):
    with pytest.raises(ValueError):
        sp.save_template(STRATEGY_CONSERVATIVE, "   ", {"sl_pt": 3.0})


def test_save_template_unknown_strategy_raises(fresh_db):
    with pytest.raises(ValueError):
        sp.save_template("not_a_real_strategy", "Name", {"sl_pt": 3.0})


def test_delete_template_removes_it(fresh_db):
    tid = sp.save_template(STRATEGY_CONSERVATIVE, "Temp", {"sl_pt": 3.0})
    sp.delete_template(tid)
    assert sp.list_templates(STRATEGY_CONSERVATIVE) == []


def test_apply_template_sets_live_values(fresh_db):
    tid = sp.save_template(STRATEGY_SCALP_RUNNER, "Wide", {"sl_pt": 20.0, "tp1_pt": 5.0, "tp2_pt": 6.0})
    applied = sp.apply_template(tid)
    assert applied["sl_pt"] == 20.0
    assert sp.get_strategy_params(STRATEGY_SCALP_RUNNER)["sl_pt"] == 20.0


def test_apply_template_missing_id_raises(fresh_db):
    with pytest.raises(ValueError):
        sp.apply_template(999999)
