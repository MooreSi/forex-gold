"""Standardised RESULTS.md writer — every experiment ends by calling
write_results(), so results are comparable at a glance across notebooks.

Template (fixed order, do not improvise):
    # RESULTS — <experiment>
    date, price source, data window
    ## Headline        one plain-English sentence Simon can read
    ## Numbers         metrics.summarize() table, one row per config
    ## Chart           equity_curve.png (cumulative R per trade)
    ## Caveats         sample-size / price-source / anything that weakens it
    ## Verdict         KEEP / DROP / NEEDS-M1 / NEEDS-MORE-DATA + one line

Chart rules (from the dataviz method): one axis, thin 2px lines, recessive
grid, validated categorical palette in fixed slot order (blue, orange, aqua,
yellow), direct labels at line ends plus a legend, text in ink colors never
series colors. Baseline is always slot 1 (blue).
"""
from __future__ import annotations

from datetime import date
from pathlib import Path

import pandas as pd

# Validated categorical palette (light surface), fixed slot order.
PALETTE = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100"]
INK, INK_MUTED, GRID = "#1a1a19", "#6b6a63", "#e6e5df"


def equity_chart(series: dict[str, list[float]], path: Path, title: str) -> None:
    """Cumulative-R equity curves, one line per config (max 4)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    if len(series) > len(PALETTE):
        raise ValueError("max 4 series — fold the rest or make two charts")

    fig, ax = plt.subplots(figsize=(9, 4.5), dpi=150)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    for i, (name, r_list) in enumerate(series.items()):
        eq = np.cumsum(np.asarray(list(r_list), dtype=float))
        x = np.arange(1, len(eq) + 1)
        ax.plot(x, eq, color=PALETTE[i], linewidth=2, label=name)
        if len(eq):
            ax.annotate(f" {name} ({eq[-1]:+.1f}R)", (x[-1], eq[-1]),
                        color=INK, fontsize=9, va="center")
    ax.axhline(0, color=INK_MUTED, linewidth=1, alpha=0.6)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    for s in ("left", "bottom"):
        ax.spines[s].set_color(GRID)
    ax.tick_params(colors=INK_MUTED, labelsize=9)
    ax.set_xlabel("trade #", color=INK_MUTED, fontsize=9)
    ax.set_ylabel("cumulative R", color=INK_MUTED, fontsize=9)
    ax.set_title(title, color=INK, fontsize=11, loc="left")
    ax.legend(frameon=False, fontsize=9, labelcolor=INK)
    ax.margins(x=0.12)  # room for the end labels
    fig.tight_layout()
    fig.savefig(path, bbox_inches="tight")
    plt.close(fig)


def write_results(
    folder: Path,
    experiment: str,
    headline: str,
    numbers: pd.DataFrame,
    caveats: list[str],
    verdict: str,
    price_source: str = "60s series (directional only)",
    data_window: str = "2026-07-21 → 2026-07-31 snapshots",
    chart_series: dict[str, list[float]] | None = None,
    chart_title: str = "Cumulative R, in trade order",
    extra_sections: dict[str, str] | None = None,
) -> Path:
    """Write RESULTS.md (and equity_curve.png) into `folder`. Returns the path."""
    folder = Path(folder)
    out_dir = folder / "output"
    out_dir.mkdir(exist_ok=True)

    chart_line = "_no chart for this experiment_"
    if chart_series:
        png = out_dir / "equity_curve.png"
        equity_chart(chart_series, png, chart_title)
        chart_line = f"![equity curves](output/equity_curve.png)"

    md = [
        f"# RESULTS — {experiment}",
        "",
        f"*Run {date.today().isoformat()} · price source: {price_source} · data: {data_window}*",
        "",
        "## Headline",
        "",
        headline,
        "",
        "## Numbers",
        "",
        numbers.to_markdown(),
        "",
        "## Chart",
        "",
        chart_line,
        "",
    ]
    for name, body in (extra_sections or {}).items():
        md += [f"## {name}", "", body, ""]
    md += ["## Caveats", ""]
    md += [f"- {c}" for c in caveats]
    md += ["", "## Verdict", "", verdict, ""]

    path = folder / "RESULTS.md"
    path.write_text("\n".join(md), encoding="utf-8")
    return path
