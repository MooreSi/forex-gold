"""The "Is it learning?" chart, and why it never moved.

Reported from the screen 2026-09-16: the Reversal Engine's chart "has never
updated/changed". It was not broken. It was plotting a CUMULATIVE mean:

    win_rate_series[i] = wins / (i + 1) * 100

With 4,880 labelled signals, point 4,880 moves that mean by 1/4880, about
0.02%, and 4,880 points were being squeezed into 280 pixels. A cumulative
average over a five-figure sample is a constant with extra steps: it cannot
answer "is it learning?", because it weighs a signal from July exactly as
heavily as one from this morning.

The Breakout engine has 123 labelled signals, so the same code was still
early enough on the same curve to move -- and it was moving DOWN, which is a
real finding rather than a rendering artefact.

A rolling window answers the question the title asks. `rolling_mean` is the
whole of the change; everything else here is labelling.
"""
from __future__ import annotations

import pytest
from nicegui import ui
from nicegui.client import Client

from frontend.components import learning_chart as lc


class TestTheRollingWindow:
    def test_it_averages_only_the_last_window(self):
        series = [0.0] * 100 + [1.0] * 50
        assert lc.rolling_mean(series, 50)[-1] == pytest.approx(1.0)

    def test_early_points_average_what_exists_rather_than_padding(self):
        """Padding with zeros would draw a fake climb out of the floor on
        every engine's first fifty signals."""
        assert lc.rolling_mean([1.0, 1.0, 1.0], 50) == pytest.approx([1.0, 1.0, 1.0])

    def test_it_returns_one_value_per_input_point(self):
        assert len(lc.rolling_mean([1.0] * 37, 10)) == 37

    def test_an_empty_series_gives_an_empty_result(self):
        assert lc.rolling_mean([], 50) == []

    def test_it_tracks_a_change_a_cumulative_mean_would_bury(self):
        """The regression this module exists for, at the real sample size.

        4,830 wins then 50 straight losses. The cumulative mean still reads
        like a 99% win rate; the rolling window reads the collapse.
        """
        series = [100.0] * 4830 + [0.0] * 50
        cumulative = sum(series) / len(series)
        rolled = lc.rolling_mean(series, lc.WINDOW)[-1]
        assert cumulative > 98.0
        assert rolled == pytest.approx(0.0)

    def test_the_window_is_fifty(self):
        assert lc.WINDOW == 50


class TestWhatItPlots:
    def test_it_plots_the_tail_not_the_whole_history(self):
        """4,880 points in 280px is 17 points per pixel. The line is drawn
        from the last `PLOT_POINTS` rolling values so it has visible shape
        and shifts when a signal closes."""
        assert lc.PLOT_POINTS <= 200
        pts = lc.svg_points([float(i) for i in range(4880)], 0, 5000, 280, 50)
        assert len(pts.split()) <= lc.PLOT_POINTS

    def test_a_flat_scale_does_not_divide_by_zero(self):
        assert lc.svg_points([5.0, 5.0], 5.0, 5.0, 280, 50) == ""

    def test_none_values_are_skipped_rather_than_plotted_as_zero(self):
        pts = lc.svg_points([1.0, None, 1.0], 0.0, 2.0, 280, 50)
        assert len(pts.split()) == 2


class TestTheChartSaysWhatTheAxesAre:
    """The owner's question was "what does the X and Y mean, should the lines
    converge?". They should not: they are two unrelated quantities on two
    different scales. Nothing on the old chart said so."""

    def _render(self, metrics):
        with Client(lambda: None, request=None):
            container = ui.column()
            with container:
                lc.render(metrics)
            return container

    def _metrics(self, n=200):
        return {
            "signal_ids": [f"S{i}" for i in range(n)],
            "win_flag_series": [1, 0] * (n // 2),
            "actual_r_series": [0.1, -0.2] * (n // 2),
            "pred_r_series": [0.05] * n,
        }

    def _text(self, container) -> str:
        out = []
        def walk(el):
            t = getattr(el, "text", None)
            if t:
                out.append(str(t))
            for child in el.default_slot.children:
                walk(child)
        walk(container)
        return " ".join(out)

    def test_it_names_the_window_in_the_heading(self):
        assert "50" in self._text(self._render(self._metrics()))

    def test_it_labels_both_y_scales(self):
        text = self._text(self._render(self._metrics()))
        assert "%" in text
        assert "R" in text

    def test_it_says_the_x_axis_is_signals_not_time(self):
        """Signals are not evenly spaced in time. Reading this as a time
        series is the most likely wrong conclusion to draw from it."""
        assert "signal" in self._text(self._render(self._metrics())).lower()

    def test_it_says_the_two_lines_are_not_comparable(self):
        text = self._text(self._render(self._metrics())).lower()
        assert "scale" in text or "not comparable" in text

    def test_with_no_data_it_says_so_rather_than_drawing_an_empty_box(self):
        text = self._text(self._render({"signal_ids": []}))
        assert "no" in text.lower()


class TestTheLegendMatchesTheTable:
    def test_actual_r_is_not_the_colour_the_table_uses_for_predicted_r(self):
        """On the old panel the legend called ORANGE "actual R" while the
        table three centimetres below coloured PREDICTED R orange and actual
        R purple. Same colour, two meanings, one card."""
        assert lc.COLOUR_ACTUAL_R != lc.COLOUR_PRED_R

    def test_the_table_and_the_chart_agree_on_actual_r(self):
        assert lc.COLOUR_ACTUAL_R == lc.TABLE_COLOUR_ACTUAL_R
