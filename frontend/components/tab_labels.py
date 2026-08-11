"""Plain-language subtitles for the ten top-level tabs.

The tab names themselves are load-bearing (pages and handlers compare
against them), so instead of renaming, every tab gets a one-line subtitle
rendered as its tooltip. The four jargon names — Parsing, Signal
Generator, Edge, Analysis — get real explanations.

Data only; the shell applies these in one loop.
"""
from __future__ import annotations

from typing import Mapping, Optional

TAB_SUBTITLES: dict[str, str] = {
    "AI Analysis": "AI market research and trade commentary",
    "Chart": "Live XAUUSD price chart",
    "Trading": "Signals, open positions and strategy settings",
    "Parsing": "Telegram signal reader — where channel signals arrive",
    "Signal Generator": "The app's own strategy engines (breakout, bounce, reversal)",
    "Edge": "Live message trace — follow a signal through the system",
    "Backtest": "Replay signals against history to test settings",
    "Analysis": "Trade history and performance stats",
    "Settings": "Connections, risk, alerts and app configuration",
    "About": "Guides, setup instructions and the glossary",
}


def missing_subtitles(
    tab_names: list[str],
    subtitles: Optional[Mapping[str, str]] = None,
) -> list[str]:
    """Return the tabs that have no non-blank subtitle. Empty list = all good."""
    subs = TAB_SUBTITLES if subtitles is None else subtitles
    return [name for name in tab_names if not (subs.get(name) or "").strip()]
