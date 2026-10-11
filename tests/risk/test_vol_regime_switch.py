"""The volatility-regime feature switch: off by default, reachable, out of
the AI's hands. docs/todo/016 phase 2.

On, the Reversal ML model learns and scores with one more input: ATR against
its own five-day median (`vol_regime`). That changes which signals the ML
gate passes, so it is the owner's switch, flipped on demo first.

Nothing here reaches a broker.
"""
from pathlib import Path

from backend.src.services.risk import capability_gates as caps

_ROOT = Path(__file__).resolve().parents[2]
_CARD = _ROOT / "frontend/src/components/engines/content/capabilities.ts"
_KEY = "re_vol_regime_feature_enabled"


class TestTheDefaultIsOff:
    def test_an_empty_row_reads_as_off(self):
        assert caps.vol_regime_feature_enabled({}) is False

    def test_a_null_column_reads_as_off(self):
        assert caps.vol_regime_feature_enabled({_KEY: None}) is False


class TestItIsWired:
    def test_on_is_on(self):
        assert caps.vol_regime_feature_enabled({_KEY: 1}) is True

    def test_it_reads_its_own_column_not_a_neighbour(self):
        assert caps.vol_regime_feature_enabled(
            {_KEY: 0, "vol_target_sizing_enabled": 1,
             "re_xasset_features_enabled": 1}) is False


class TestItIsReachable:
    def test_a_migration_adds_the_column_defaulting_to_off(self):
        from backend.migrations.steps import MIGRATIONS
        adds = [stmt for _n, _t, step in MIGRATIONS
                if isinstance(step, (list, tuple))
                for stmt in step
                if isinstance(stmt, str) and f"ADD COLUMN {_KEY}" in stmt]
        assert len(adds) == 1, adds
        assert "vantage_risk_settings" in adds[0]
        assert "DEFAULT 0" in adds[0]

    def test_it_syncs_to_the_paired_node(self):
        from backend.src.services.cluster.sync import synced_settings
        src = Path(synced_settings.__file__).read_text(encoding="utf-8")
        assert f'"{_KEY}"' in src

    def test_the_card_carries_it_and_says_to_try_it_on_demo(self):
        src = _CARD.read_text(encoding="utf-8")
        assert f'"{_KEY}"' in src
        near = src[src.index(f'"{_KEY}"'):][:1200]
        assert "demo" in near.lower()


class TestTheAiCannotTouchIt:
    def test_not_in_the_tuner_allowlist(self):
        from backend.src.services.reversal_engine import ai_tuner
        assert _KEY not in ai_tuner.TUNABLE
