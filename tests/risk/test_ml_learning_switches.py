"""The four docs/todo/017 learning switches: off by default, reachable, each
reading its own column, and out of the AI tuner's hands.

Every one of them changes which signals an ML gate passes once on, so each is
the owner's switch, flipped on demo first. Off, the models fit exactly as
before (pinned in the engine tests).

Nothing here reaches a broker.
"""
from pathlib import Path

import pytest

from backend.src.services.risk import capability_gates as caps

_ROOT = Path(__file__).resolve().parents[2]
_CARD = _ROOT / "frontend/src/components/engines/content/capabilities.ts"

SWITCHES = [
    ("re_ml_time_decay_enabled", caps.re_ml_time_decay_enabled),
    ("bo_ml_time_decay_enabled", caps.bo_ml_time_decay_enabled),
    ("re_ml_learn_from_executed", caps.re_ml_learn_from_executed),
    ("bo_ml_realised_r_label", caps.bo_ml_realised_r_label),
]
KEYS = [k for k, _ in SWITCHES]


@pytest.mark.parametrize("key,read", SWITCHES)
class TestEachSwitch:
    def test_an_empty_row_reads_as_off(self, key, read):
        assert read({}) is False

    def test_a_null_column_reads_as_off(self, key, read):
        assert read({key: None}) is False

    def test_on_is_on(self, key, read):
        assert read({key: 1}) is True

    def test_it_reads_its_own_column_not_a_neighbour(self, key, read):
        others = {k: 1 for k in KEYS if k != key}
        assert read({key: 0, **others}) is False

    def test_a_migration_adds_the_column_defaulting_to_off(self, key, read):
        from backend.migrations.steps import MIGRATIONS
        adds = [stmt for _n, _t, step in MIGRATIONS
                if isinstance(step, (list, tuple))
                for stmt in step
                if isinstance(stmt, str) and f"ADD COLUMN {key} " in stmt]
        assert len(adds) == 1, adds
        assert "vantage_risk_settings" in adds[0]
        assert "DEFAULT 0" in adds[0]

    def test_it_syncs_to_the_paired_node(self, key, read):
        from backend.src.services.cluster.sync import synced_settings
        assert key in synced_settings.SYNCED_SETTINGS_KEYS

    def test_the_card_carries_it_and_says_to_try_it_on_demo(self, key, read):
        src = _CARD.read_text(encoding="utf-8")
        assert f'"{key}"' in src
        near = src[src.index(f'"{key}"'):][:1400]
        assert "demo" in near.lower()

    def test_not_in_the_tuner_allowlist(self, key, read):
        from backend.src.services.reversal_engine import ai_tuner
        assert key not in ai_tuner.TUNABLE
