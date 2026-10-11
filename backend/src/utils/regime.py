"""
Frozen hour and level blocklists for the signal engines.

Built from measured per-day performance (279 bounce + 231 breakout closed
signals, 2026-06-15 → 2026-07-06) correlated against MT5 XAUUSD daily data.
The measurements behind each set are recorded beside it. They are frozen to
that window; docs/todo/004 measures where they and newer data disagree.

Read today by the Breakout engine (its hour and level sets) and by
`services/analytics/regime_drift.py` (all four). The Bounce sets outlived the
Bounce engine (deleted 2026-09-14) because regime_drift still reports on them.

This module used to hold a five-state day classifier (`classify_day`,
`efficiency_ratio`, `bounce_pattern_allowed`). Its only caller was the Bounce
engine, so it was deleted with it on 2026-10-11 (docs/todo/016). The live
regime readers are listed in docs/system/domains/engines/README.md.

All values are constants. Nothing here holds state.
"""
from __future__ import annotations

# ── Per-engine hour blocks (UTC) ─────────────────────────────────────────────
# Measured cumulative PnL by signal-creation hour (Mac node, all history):
#   bounce:   07h -$358 | 12h -$124 | 13h -$228 | 14h -$139 | 15h -$141 |
#             16h -$181 | 20h -$206   → combined -$1,377 across 7 hours
#   breakout: 12h -$351 | 13h -$457 | 14h -$272 → combined -$1,079
# London morning (08-11) and Asian hours carried the profitable trades for
# both engines and stay open.
BOUNCE_BLOCKED_HOURS_UTC:   frozenset[int] = frozenset({7, 12, 13, 14, 15, 16, 20})
BREAKOUT_BLOCKED_HOURS_UTC: frozenset[int] = frozenset({12, 13, 14})

# ── Level-type filters ───────────────────────────────────────────────────────
# Bounce entries AT these level types lost consistently (they break more often
# than they hold — internally consistent with the breakout engine's data where
# BREAKS of liq_low were its single best trade at +$387/69% WR):
#   liq_low -$744 (37% WR) | fvg_bear -$515 | fvg_bull -$320 | liq_high -$106
# They remain valid as TP targets and for breakout trading — only bounce
# ENTRIES at them are blocked.
BOUNCE_BLOCKED_ENTRY_LEVELS: frozenset[str] = frozenset(
    {"liq_low", "liq_high", "fvg_bull", "fvg_bear"}
)

# Breakouts THROUGH these level types lost consistently:
#   fvg_bull -$793 (37% WR) | round -$223 (36% WR)
BREAKOUT_BLOCKED_LEVELS: frozenset[str] = frozenset({"fvg_bull", "round"})
