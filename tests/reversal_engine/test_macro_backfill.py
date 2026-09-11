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

    def test_a_short_pre_v9_vector_needs_backfilling_too(self):
        """**Corrected 2026-09-11 against live data.** This originally
        asserted the opposite, on the reasoning that a 33-wide vector has
        no macro slots so padding it here would duplicate what
        `_training_data` does at training time.

        Running the study on the real database showed why that was wrong:
        3,359 of 5,000 stored vectors are 33-wide and only 698 are 38-wide,
        so skipping short ones made the repair a no-op on 96% of exactly
        the population data-inspect/003 identified. The padding
        `_training_data` applies is neutral-filled and thrown away after
        every training run; repairing the stored row puts REAL values in
        it, once."""
        assert mb.needs_backfill([0.0] * 33) is True

    def test_a_vector_longer_than_the_schema_is_still_skipped(self):
        """From a newer build. It cannot be interpreted, and padding runs
        one way only."""
        assert mb.needs_backfill([0.0] * 40) is False


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

    def test_a_short_vector_is_widened_and_its_macro_slots_filled(self):
        """Widened with the documented neutral for every feature added
        between its own width and the macro block, exactly as
        `_training_data` would -- then the macro five get REAL values."""
        rows = [{"id": 3, "created_at": T0 + HOUR,
                 "ml_features_json": json.dumps([0.5] * 33)}]
        written = []
        report = mb.backfill(rows, series({"^VIX": [18.0, 44.0]}),
                             write_fn=written.append, apply=True)
        assert report["updated"] == 1
        _sig_id, vector = written[0]
        assert len(vector) == 38
        vix_idx = 33 + MACRO_FEATURE_NAMES.index("vix_level")
        assert vector[vix_idx] == pytest.approx(1.0)
        assert vector[:33] == [0.5] * 33

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


class TestReadingWhatYahooActuallyReturns:
    """**Found by running it, 2026-09-11.** The first version of
    `fetch_history` did `row["Close"]` over `df.iterrows()`. yfinance 1.7
    returns MultiIndex columns -- `('Close', '^VIX')` -- even for a single
    ticker, so that expression yields a one-element Series rather than a
    float, `float()` on it raised, the broad `except` swallowed it, and
    every symbol came back empty. The repair reported 4,288 rows with "no
    data" and wrote nothing, which looked exactly like Yahoo having no
    history for the period.

    A guessed dataframe shape, in other words. The seam is extracted and
    tested so the guess cannot recur silently.
    """

    def _frame(self, multiindex: bool):
        pd = pytest.importorskip("pandas")
        idx = pd.to_datetime(["2026-09-01T09:00:00Z", "2026-09-01T10:00:00Z"])
        if multiindex:
            cols = pd.MultiIndex.from_tuples(
                [("Close", "^VIX"), ("High", "^VIX")])
            return pd.DataFrame([[15.1, 15.4], [16.2, 16.5]], index=idx, columns=cols)
        return pd.DataFrame({"Close": [15.1, 16.2], "High": [15.4, 16.5]}, index=idx)

    def test_it_reads_multiindex_columns(self):
        out = mb._closes_from_frame(self._frame(multiindex=True))
        assert [c for _ts, c in out] == pytest.approx([15.1, 16.2])

    def test_it_still_reads_a_flat_frame(self):
        out = mb._closes_from_frame(self._frame(multiindex=False))
        assert [c for _ts, c in out] == pytest.approx([15.1, 16.2])

    def test_the_timestamps_come_back_as_unix_seconds(self):
        """Derived, not hardcoded: the first version of this assertion
        carried a hand-typed epoch that was a day out, and a wrong constant
        in a test about timestamps is the least useful kind of wrong."""
        import datetime as dt
        expected = dt.datetime(2026, 9, 1, 9, 0, tzinfo=dt.timezone.utc).timestamp()
        out = mb._closes_from_frame(self._frame(multiindex=True))
        assert out[0][0] == pytest.approx(expected)
        assert out[1][0] - out[0][0] == pytest.approx(3600.0)

    def test_a_frame_with_no_close_column_yields_nothing(self):
        pd = pytest.importorskip("pandas")
        df = pd.DataFrame({"Open": [1.0]}, index=pd.to_datetime(["2026-09-01T09:00:00Z"]))
        assert mb._closes_from_frame(df) == []

    def test_rows_with_no_price_are_dropped_rather_than_read_as_zero(self):
        """A NaN hour is a gap in the feed. Reading it as 0.0 would put a
        VIX of zero into the training set."""
        pd = pytest.importorskip("pandas")
        idx = pd.to_datetime(["2026-09-01T09:00:00Z", "2026-09-01T10:00:00Z"])
        df = pd.DataFrame({"Close": [float("nan"), 16.2]}, index=idx)
        assert [c for _ts, c in mb._closes_from_frame(df)] == pytest.approx([16.2])

    def test_an_empty_or_missing_frame_is_not_an_error(self):
        assert mb._closes_from_frame(None) == []
