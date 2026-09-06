"""ml_engine v9: the five macro features and the back-fill of older rows.

Spec: docs/todo/001-reversal-macro-context.md. Pure-function tests -- no model
is trained, no DB is written, nothing places or modifies a trade.

The one that matters here is the back-fill. `_get_training_data` right-pads a
stored vector written under an earlier `_version` up to the current width, so
a feature addition does not throw the training history away. That padding is
only truthful if `_FEATURE_NEUTRAL` carries an entry for every new name: a
missing key pads with 0.0, and 0.0 in the `us10y_level` slot tells the model
the ten-year was at zero for all ~576 historical signals.
"""
import json

import pytest

from backend.src.services.reversal_engine import ml_engine as m
from backend.src.services.reversal_engine import re_macro


class TestTheV9Contract:
    def test_the_vector_is_38_wide(self):
        assert len(m.FEATURE_NAMES) == 38

    def test_the_macro_names_are_appended_last_and_in_order(self):
        """Append-only. Padding a short old vector on the right is correct
        only while every earlier position still means what it meant."""
        assert m.FEATURE_NAMES[-5:] == re_macro.MACRO_FEATURE_NAMES
        assert m.FEATURE_NAMES[-6] == "pro_likeness"

    def test_the_version_was_bumped(self):
        """Without this a v8 pickle loads against a 38-wide schema and every
        predict() call is a dimension mismatch."""
        assert m._version == "re_ml_v9"

    def test_every_macro_name_has_a_neutral(self):
        for name in re_macro.MACRO_FEATURE_NAMES:
            assert name in m._FEATURE_NEUTRAL

    def test_the_neutrals_are_the_normalised_ones(self):
        assert m._FEATURE_NEUTRAL["us10y_level"] == pytest.approx(0.75)
        assert m._FEATURE_NEUTRAL["vix_level"] == pytest.approx(0.5)
        assert m._FEATURE_NEUTRAL["gvz_level"] == pytest.approx(0.425)


class TestExtraction:
    def test_macro_values_land_in_the_last_five_slots(self):
        feats = m.extract_features({
            "level_type": "asia_low",
            "dxy_momentum": -0.4, "us10y_level": 3.0, "vix_level": 30.0,
            "gvz_level": 20.0, "tip_momentum": 0.25,
        })
        assert feats[-5:] == pytest.approx([-0.4, 0.5, 0.75, 0.5, 0.25])

    def test_a_signal_with_no_macro_gets_the_neutrals_not_zeros(self):
        feats = m.extract_features({"level_type": "asia_low"})
        assert len(feats) == 38
        assert feats[-5:] == pytest.approx(
            [m._FEATURE_NEUTRAL[n] for n in re_macro.MACRO_FEATURE_NAMES]
        )


class TestBackFillOfOlderRows:
    @staticmethod
    def _rows(width):
        return [{
            "ml_features_json": json.dumps([0.5] * width),
            "outcome": "loss",
            "sl_dist": 7.0,
            "net_pnl_dollars": -70.0,
        }]

    def _load(self, monkeypatch, width):
        from backend.src.services.reversal_engine import reversal_engine_repo as re_db
        monkeypatch.setattr(re_db, "get_ml_training_data", lambda: self._rows(width))
        return m._get_training_data()

    def test_a_v8_row_is_padded_with_the_macro_neutrals(self, monkeypatch):
        X, y = self._load(monkeypatch, 33)
        assert len(X) == 1
        assert len(X[0]) == 38
        assert X[0][-5:] == pytest.approx(
            [m._FEATURE_NEUTRAL[n] for n in re_macro.MACRO_FEATURE_NAMES]
        )
        assert y[0] == pytest.approx(-1.0)

    def test_the_padding_is_not_a_row_of_zeros(self, monkeypatch):
        """The failure mode this whole file exists for. Three of the five
        neutrals are non-zero, so a missing _FEATURE_NEUTRAL entry is visible
        here and nowhere else."""
        X, _ = self._load(monkeypatch, 33)
        assert X[0][-5:] != pytest.approx([0.0] * 5)

    def test_a_current_width_row_is_untouched(self, monkeypatch):
        X, _ = self._load(monkeypatch, 38)
        assert X[0] == pytest.approx([0.5] * 38)

    def test_a_wider_row_from_a_newer_build_is_still_skipped(self, monkeypatch):
        """A vector longer than the schema cannot be interpreted. It must be
        dropped, not truncated."""
        X, _ = self._load(monkeypatch, 39)
        assert X == []


class TestEngineIsolation:
    def test_breakout_and_bounce_vectors_are_unchanged(self):
        """Engines share no ML labels or parameters. The macro read is a
        shared *import*, not shared state."""
        from backend.src.services.breakout_signal import ml_engine as bo
        from backend.src.services.test_signal import ml_engine as ts
        assert len(bo.FEATURE_NAMES) == 22
        assert len(ts.FEATURE_NAMES) == 42
