"""The "Is it learning?" chart, shared by both engine panels.

Reported from the screen on 2026-09-16: the Reversal Engine's copy of this
chart had never visibly changed. It was not broken, it was answering a
different question. Both panels plotted a **cumulative** mean:

    win_rate_series[i] = wins / (i + 1) * 100

The Reversal Engine has 4,880 labelled signals. Point 4,880 moves that mean
by 1/4880, about 0.02%, and 4,880 points were mapped into 280 pixels at 17
points per pixel. A cumulative average over a five-figure sample is a
constant with extra steps, and it cannot answer "is it learning?" because it
weighs a signal from July exactly as heavily as one from this morning.

The Breakout engine has 123 labelled signals, early enough on the same curve
to still move -- and it was moving down. That reading was real.

## What the axes are

The owner's question was whether the two lines should converge. **No.** They
are unrelated quantities drawn on two different scales in one box, which is
why this version labels both and says so on the card:

  * **X** -- closed signals, oldest to newest. NOT time: signals are not
    evenly spaced, and reading this as a time series is the most likely
    wrong conclusion to draw from it.
  * **Y, green** -- win rate over the last `WINDOW` closed signals, on a
    0-100% scale. Above the midline is better than a coin.
  * **Y, orange** -- mean realised R over the same window, on a -1..+1
    scale. Above the midline is positive expectancy.

A rising win rate with a falling R is both possible and important -- it is
what "more small wins, fewer but larger losses" looks like -- and the old
chart made it nearly impossible to see.

## The colour collision this also fixes

The old legend called orange "actual R", while the table immediately below
coloured PREDICTED R orange and actual R purple. Same colour, two meanings,
one card. The constants below are shared with that table so the two cannot
drift apart again.
"""
from __future__ import annotations

from typing import Any, Optional, Sequence

from nicegui import ui

# Rolling window, in closed signals. Long enough that one trade does not
# swing the line, short enough that a month-old trade has left it.
WINDOW = 50

# How many rolling values to draw. The rolling mean is computed over the
# whole history; only the tail is plotted, so the line has visible shape and
# shifts when a signal closes instead of being 17 points deep per pixel.
PLOT_POINTS = 120

W, H = 280, 50

COLOUR_WIN_RATE = "#4ade80"   # green-400
COLOUR_ACTUAL_R = "#c084fc"   # purple-400, matching the table's Act R column
COLOUR_PRED_R   = "#fb923c"   # orange-400, the table's Pred R column
TABLE_COLOUR_ACTUAL_R = "#c084fc"


def rolling_mean(series: Sequence[Optional[float]],
                 window: int = WINDOW) -> list[float]:
    """Mean of the last `window` values at each point.

    Before `window` points exist it averages what there is, rather than
    padding with zeros -- padding would draw a fake climb out of the floor
    across every engine's first fifty signals.
    """
    out: list[float] = []
    run: list[float] = []
    for v in series:
        if v is None:
            out.append(out[-1] if out else 0.0)
            continue
        run.append(float(v))
        if len(run) > window:
            del run[0]
        out.append(sum(run) / len(run))
    return out


def svg_points(series: Sequence[Optional[float]], lo: float, hi: float,
               w: int = W, h: int = H) -> str:
    """The tail of `series` as SVG polyline points."""
    if not series or hi == lo:
        return ""
    tail = list(series)[-PLOT_POINTS:]
    pts = []
    for i, v in enumerate(tail):
        if v is None:
            continue
        x = int(i / max(len(tail) - 1, 1) * w)
        y = int(h - (float(v) - lo) / (hi - lo) * h)
        pts.append(f"{x},{y}")
    return " ".join(pts)


def render(metrics: dict) -> None:
    """Draw the chart into the current slot. Renders nothing but a note when
    there is no closed-signal history to plot."""
    sig_ids = metrics.get("signal_ids") or []
    if not sig_ids:
        ui.label("No closed signals with a stored ML probability yet, "
                 "so there is nothing to plot.").classes(
            "text-xs text-gray-600 italic mt-1")
        return

    ui.label(f"Is it learning?  (rolling {WINDOW} closed signals)").classes(
        "text-xs font-semibold text-gray-400 uppercase tracking-wider mt-1")

    win_flags = metrics.get("win_flag_series") or []
    actual_r = metrics.get("actual_r_series") or []

    wr_series = [f * 100.0 for f in rolling_mean(win_flags)]
    ar_series = rolling_mean(actual_r)

    wr_pts = svg_points(wr_series, 0.0, 100.0)
    ar_pts = svg_points(ar_series, -1.0, 1.0)

    svg = f"""<svg width="{W}" height="{H}" viewBox="0 0 {W} {H}"
                   xmlns="http://www.w3.org/2000/svg"
                   style="background:#1f2937;border-radius:4px">
          <line x1="0" y1="{H // 2}" x2="{W}" y2="{H // 2}"
                stroke="#374151" stroke-width="1" stroke-dasharray="4,4"/>
          {f'<polyline points="{wr_pts}" fill="none" stroke="{COLOUR_WIN_RATE}" stroke-width="1.5"/>' if wr_pts else ''}
          {f'<polyline points="{ar_pts}" fill="none" stroke="{COLOUR_ACTUAL_R}" stroke-width="1.5" stroke-dasharray="3,2"/>' if ar_pts else ''}
        </svg>"""
    ui.html(svg)

    with ui.row().classes("gap-3 text-xs"):
        ui.label("— win rate, 0-100% scale").style(f"color:{COLOUR_WIN_RATE}")
        ui.label("--- mean realised R, -1..+1 scale").style(
            f"color:{COLOUR_ACTUAL_R}")
    ui.label(
        "X: closed signals, oldest to newest (not time — signals are not "
        "evenly spaced). Dashed midline is 50% for win rate and 0.0 for R. "
        "The two lines use different scales and are not comparable to each "
        "other; read each against the midline."
    ).classes("text-xs text-gray-600 leading-relaxed")
