"""Per-tier event windows, and the asymmetry that matters.

Section 5.6 of `docs/todo/reversal-engine/200`.

`news_calendar.get_blackout_settings()` gives ONE before/after window for
whichever impact threshold is configured, so an FOMC statement and a middling
trade-balance print are treated identically. They are not, and the danger is
not symmetric either: the twenty minutes AFTER a release -- spread wide, book
thin, stops being reached -- is worse than the twenty before it, when at
least nothing has happened yet.

This composes OVER `news_calendar`'s events. It does not fetch, re-rate or
re-weight anything: the feed, the gold currency weighting and the impact
ranks stay in one place, and this only decides how wide a window each tier
earns.
"""
from __future__ import annotations

from dataclasses import dataclass, field

# Title fragments, lowercase, matched against a HIGH-impact event only. A
# match cannot promote a low- or medium-rated event: without that rule a
# "Fed Member Speaks" footnote becomes a full FOMC blackout.
TIER_1_KEYWORDS = (
    "fomc", "federal funds", "rate decision", "interest rate decision",
    "cpi", "consumer price", "core pce", "non-farm", "nonfarm",
    "powell", "fed chair",
)
TIER_2_KEYWORDS = (
    "ppi", "producer price", "retail sales", "gdp", "ism",
    "unemployment rate", "unemployment claims", "jolts", "pce",
)


@dataclass(frozen=True)
class Config:
    enabled: bool = True
    # (minutes before, minutes after) per tier. After is wider than before
    # in every tier, on purpose.
    windows: dict = field(default_factory=lambda: {
        1: (60, 90),
        2: (30, 45),
        3: (10, 20),
    })

    def window_for(self, tier: int) -> tuple[int, int]:
        return self.windows.get(int(tier), self.windows[3])


def tier_of(event: dict) -> int:
    """1 (moves gold on its own), 2 (second rank), 3 (everything else)."""
    if str(event.get("impact", "")).strip().lower() != "high":
        return 3
    title = str(event.get("title", "")).lower()
    if any(k in title for k in TIER_1_KEYWORDS):
        return 1
    if any(k in title for k in TIER_2_KEYWORDS):
        return 2
    return 3


def check(events, cfg: Config) -> tuple[bool, str]:
    """`(allowed, reason)` over `news_calendar`-shaped events.

    Each event carries `mins_until`: positive before it, negative after.
    When several windows overlap, the lowest tier is the one reported --
    being told about the trade-balance print while FOMC is ten minutes away
    would be technically true and useless.
    """
    if not cfg.enabled or not events:
        return True, ""

    blocking = []
    for ev in events:
        tier = tier_of(ev)
        before, after = cfg.window_for(tier)
        try:
            mins = float(ev.get("mins_until"))
        except (TypeError, ValueError):
            continue
        if mins >= 0 and mins <= before:
            blocking.append((tier, mins, ev, f"in {int(round(mins))} min"))
        elif mins < 0 and -mins <= after:
            blocking.append((tier, -mins, ev,
                             f"{int(round(-mins))} min since"))

    if not blocking:
        return True, ""

    blocking.sort(key=lambda b: (b[0], b[1]))
    tier, _mins, ev, when = blocking[0]
    return False, (f"Event blackout (tier {tier}) - {ev.get('title', 'event')} "
                   f"({ev.get('currency', '')}), {when}")
