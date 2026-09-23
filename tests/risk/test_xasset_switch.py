"""The cross-asset switch: off by default, reachable, out of the AI's hands.

docs/todo/reversal-engine/230. The switch decides only whether the
meta-labeller is trained and scores with the cross-asset features. It places
nothing: whether the live path consults that model is still
`meta_label_gate_enabled`.
"""
from pathlib import Path

from backend.src.services.risk import capability_gates as caps

_ROOT = Path(__file__).resolve().parents[2]
_CARD = _ROOT / "frontend/src/components/engines/content/capabilities.ts"


class TestTheDefaultIsOff:
    def test_an_empty_row_reads_as_off(self):
        assert caps.xasset_features_enabled({}) is False

    def test_a_null_column_reads_as_off(self):
        assert caps.xasset_features_enabled({"re_xasset_features_enabled": None}) is False


class TestItIsWired:
    def test_on_is_on(self):
        assert caps.xasset_features_enabled({"re_xasset_features_enabled": 1}) is True

    def test_it_reads_its_own_column_not_a_neighbour(self):
        assert caps.xasset_features_enabled({"re_xasset_features_enabled": 0,
                                             "meta_label_gate_enabled": 1}) is False


class TestItIsReachable:
    def test_a_migration_adds_the_column_defaulting_to_off(self):
        from backend.migrations.steps import MIGRATIONS
        adds = [stmt for _n, _t, step in MIGRATIONS
                if isinstance(step, (list, tuple))
                for stmt in step
                if isinstance(stmt, str) and "ADD COLUMN re_xasset_features_enabled" in stmt]
        assert len(adds) == 1, adds
        assert "vantage_risk_settings" in adds[0]
        assert "DEFAULT 0" in adds[0]

    def test_the_card_carries_it(self):
        assert '"re_xasset_features_enabled"' in _CARD.read_text(encoding="utf-8")

    def test_the_card_says_what_it_does_not_do(self):
        """Turned on, it trades nothing differently unless the meta-labeller
        gate is on too. A card that let the owner believe otherwise is the
        failure `dependsOn` exists for."""
        src = _CARD.read_text(encoding="utf-8")
        near = src[src.index('"re_xasset_features_enabled"'):][:1200]
        assert "meta_label_gate_enabled" in near


class TestTheAiCannotTouchIt:
    def test_not_in_the_tuner_allowlist(self):
        from backend.src.services.reversal_engine import ai_tuner
        assert "re_xasset_features_enabled" not in ai_tuner.TUNABLE
