"""Give the five macro features real values in the training set.

Section 4.4 of docs/todo/reversal-engine/200. data-inspect/003 measured it:
v9 widened the feature vector from 33 to 38 and back-filled the five macro
slots with `_FEATURE_NEUTRAL` for every row written before the changeover.
Of roughly 4,050 training rows only ~175 carry real macro values, so **five
of the model's 38 inputs are a constant for 96% of what it learns from.**

A constant input cannot help and can hurt through variance. The fix is not
a sixth series; it is giving the five you have real values.

Two properties are pinned hardest:

  * the reconstruction uses the SAME arithmetic as the live path, or the
    back-filled rows and the live rows are two different features sharing
    one name
  * it is a DRY RUN by default. Rewriting stored training vectors changes
    what the live ML gate learns at its next retrain, which makes it a
    behaviour change even though it places nothing.
"""
from __future__ import annotations

import json

import pytest

from backend.src.services.reversal_engine import macro_backfill as mb
from backend.src.services.reversal_engine.re_macro import (
    MACRO_FEATURE_NAMES, MACRO_NEUTRAL)


HOUR = 3600.0
T0 = 1_788_739_200.0


def series(symbol_closes):
    """{symbol: [(ts, close), ...]}"""
    return {sym: [(T0 + i * HOUR, c) for i, c in enumerate(closes)]
            for sym, closes in symbol_closes.items()}


class TestTheArithmeticMatchesTheLivePath:
    def test_dxy_momentum_is_the_hourly_return_over_a_half_percent(self):
        """`market_context._dxy_momentum`: 0.5% in an hour is +-1.0."""
        s = series({"DX-Y.NYB": [100.0, 100.5]})
        assert mb.raw_at(T0 + HOUR, s)["dxy_momentum"] == pytest.approx(1.0)

    def test_tip_momentum_uses_its_own_narrower_scale(self):
        """0.2% for TIP, not 0.5%. Sharing the scale would make TIP look
        2.5x less significant than the live feature says it is."""
        s = series({"TIP": [100.0, 100.2]})
        assert mb.raw_at(T0 + HOUR, s)["tip_momentum"] == pytest.approx(1.0)

    def test_a_level_series_is_read_as_its_close(self):
        s = series({"^TNX": [4.1, 4.3], "^VIX": [18.0, 22.0]})
        raw = mb.raw_at(T0 + HOUR, s)
        assert raw["us10y_level"] == pytest.approx(4.3)
        assert raw["vix_level"] == pytest.approx(22.0)

    def test_it_reads_the_last_close_at_or_before_the_signal(self):
        """Never a later one. A training row that saw tomorrow's VIX is the
        purest form of the leakage purged cross-validation exists to stop."""
        s = series({"^VIX": [18.0, 22.0, 30.0]})
        assert mb.raw_at(T0 + HOUR + 60, s)["vix_level"] == pytest.approx(22.0)

    def test_a_missing_series_falls_back_to_its_neutral(self):
        raw = mb.raw_at(T0, series({}))
        assert raw["vix_level"] == pytest.approx(mb.NEUTRAL_RAW["vix_level"])

    def test_a_timestamp_before_the_series_starts_gets_no_value(self):
        s = series({"^VIX": [18.0, 22.0]})
        assert mb.raw_at(T0 - HOUR, s)["vix_level"] == pytest.approx(
            mb.NEUTRAL_RAW["vix_level"])


class TestFindingWhatNeedsFixing:
    def _vector(self, macro_values):
        head = [0.0] * (38 - len(MACRO_FEATURE_NAMES))
        return head + list(macro_values)

    def test_a_vector_whose_macro_slots_are_all_neutral_needs_backfilling(self):
        neutral = [MACRO_NEUTRAL[n] for n in MACRO_FEATURE_NAMES]
        assert mb.needs_backfill(self._vector(neutral)) is True

    def test_a_vector_carrying_a_real_reading_is_left_alone(self):
        real = [MACRO_NEUTRAL[n] for n in MACRO_FEATURE_NAMES]
        real[2] = real[2] + 0.25
        assert mb.needs_backfill(self._vector(real)) is False

    def test_a_short_pre_v9_vector_is_not_mistaken_for_a_neutral_one(self):
        """A 33-wide vector has no macro slots at all. Padding it here
        rather than in `_training_data` would apply the widening twice."""
        assert mb.needs_backfill([0.0] * 33) is False


class TestItIsADryRunUntilToldOtherwise:
    def test_by_default_it_reports_and_writes_nothing(self):
        rows = [{"id": 1, "created_at": T0 + HOUR,
                 "ml_features_json": json.dumps(
                     [0.0] * 33 + [MACRO_NEUTRAL[n] for n in MACRO_FEATURE_NAMES])}]
        written = []
        report = mb.backfill(rows, series({"^VIX": [18.0, 22.0]}),
                             write_fn=written.append)
        assert report["would_update"] == 1
        assert written == []
        assert report["dry_run"] is True

    def test_applying_it_writes_the_recomputed_vector(self):
        rows = [{"id": 7, "created_at": T0 + HOUR,
                 "ml_features_json": json.dumps(
                     [0.0] * 33 + [MACRO_NEUTRAL[n] for n in MACRO_FEATURE_NAMES])}]
        written = []
        report = mb.backfill(rows, series({"^VIX": [18.0, 44.0]}),
                             write_fn=lambda x: written.append(x),
                             apply=True)
        assert report["updated"] == 1
        sig_id, vector = written[0]
        assert sig_id == 7
        vix_idx = 33 + MACRO_FEATURE_NAMES.index("vix_level")
        assert vector[vix_idx] == pytest.approx(1.0)

    def test_a_row_it_cannot_improve_is_not_rewritten(self):
        """No series for that date means the recomputation would produce
        the same neutrals it started with. Writing them back would mark the
        row as repaired when nothing was repaired."""
        rows = [{"id": 1, "created_at": T0 + HOUR,
                 "ml_features_json": json.dumps(
                     [0.0] * 33 + [MACRO_NEUTRAL[n] for n in MACRO_FEATURE_NAMES])}]
        written = []
        report = mb.backfill(rows, series({}), write_fn=written.append,
                             apply=True)
        assert report["updated"] == 0
        assert report["no_data"] == 1

    def test_malformed_json_is_counted_and_skipped(self):
        report = mb.backfill([{"id": 1, "created_at": T0, "ml_features_json": "{"}],
                             series({}), write_fn=lambda x: None, apply=True)
        assert report["unreadable"] == 1
