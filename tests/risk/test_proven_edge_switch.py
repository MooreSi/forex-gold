"""The proven-edge switch: off by default, reachable, out of the AI's hands.

docs/todo/reversal-engine/240. On, the Reversal Engine places no order unless
its edge model has shown positive expectancy out of sample on the template's
own exits. Off, nothing about the order path changes. Nothing here reaches a
broker.
"""
from pathlib import Path

from backend.src.services.risk import capability_gates as caps

_ROOT = Path(__file__).resolve().parents[2]
_CARD = _ROOT / "frontend/src/components/engines/content/capabilities.ts"


class TestTheDefaultIsOff:
    def test_an_empty_row_reads_as_off(self):
        assert caps.require_proven_edge({}) is False

    def test_a_null_column_reads_as_off(self):
        assert caps.require_proven_edge({"re_require_proven_edge": None}) is False


class TestItIsWired:
    def test_on_is_on(self):
        assert caps.require_proven_edge({"re_require_proven_edge": 1}) is True

    def test_it_reads_its_own_column_not_a_neighbour(self):
        assert caps.require_proven_edge({"re_require_proven_edge": 0,
                                         "meta_label_gate_enabled": 1,
                                         "re_live_execution": 1}) is False


class TestItIsReachable:
    def test_a_migration_adds_the_column_defaulting_to_off(self):
        from backend.migrations.steps import MIGRATIONS
        adds = [stmt for _n, _t, step in MIGRATIONS
                if isinstance(step, (list, tuple))
                for stmt in step
                if isinstance(stmt, str) and "ADD COLUMN re_require_proven_edge" in stmt]
        assert len(adds) == 1, adds
        assert "vantage_risk_settings" in adds[0]
        assert "DEFAULT 0" in adds[0]

    def test_the_card_carries_it(self):
        assert '"re_require_proven_edge"' in _CARD.read_text(encoding="utf-8")

    def test_the_card_says_it_stops_trading_while_nothing_is_proven(self):
        """The owner must not switch this on expecting a better filter and
        find the engine silent. The card has to say that is the likely
        result."""
        src = _CARD.read_text(encoding="utf-8")
        near = src[src.index('"re_require_proven_edge"'):][:1500]
        assert "no orders" in near.lower()


class TestTheAiCannotTouchIt:
    def test_not_in_the_tuner_allowlist(self):
        from backend.src.services.reversal_engine import ai_tuner
        assert "re_require_proven_edge" not in ai_tuner.TUNABLE
