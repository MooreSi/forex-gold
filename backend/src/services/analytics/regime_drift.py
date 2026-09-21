"""Are the frozen regime constants still true? -- docs/todo/004, phase 1.

`backend/src/utils/regime.py` decides whether either signal engine may fire.
Its blocklists are `frozenset`s fitted to 279 bounce plus 231 breakout closed
signals between 2026-06-15 and 2026-07-06, and its own docstring says so. The
sample is small once it is split across 24 hours and five day-types, the window
closed over a year ago, and nothing in the system re-reads those buckets.

This module re-reads them. It changes no behaviour and proposes no value: it
recomputes the same per-cell expectancy over whatever window it is handed and
prints it beside the constant, so the question "is this still true?" has an
answer. Widening or narrowing a gate on the strength of that answer is phase 2
and waits on the owner (004 section 4A), because a gate that widens itself can
open trading hours that are shut today.

**The finding this module exists to make visible:** a blocked cell produces no
signals, so it produces no trades, so it produces no evidence, so the block can
never be shown to be wrong. Reporting `0 trades, $0` for such a cell alongside
a live cell's real numbers would read as "harmless", which is the opposite of
true. `NO_EVIDENCE_BLOCKED` is a distinct verdict for exactly that, and
`INSUFFICIENT` is kept for a cell that was merely quiet.

Pure: it takes a list of closed-signal records and returns numbers. It opens no
database and reaches no broker -- the read lives in its caller. Pinned by
tests/analytics/test_regime_drift.py.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from backend.src.utils import regime as rg

# The window `utils/regime.py`'s constants were fitted to, carried here so the
# report can print it beside its own window and let the staleness speak.
CONSTANTS_FITTED_TO = ("2026-06-15", "2026-07-06")

AGREES              = "agrees"
CONTRADICTS         = "contradicts"
INSUFFICIENT        = "insufficient"
NO_EVIDENCE_BLOCKED = "no_evidence_blocked"

# The same question as split.MIN_TRADES_PER_SIDE and handover 036: how few
# trades is too few. Imported rather than duplicated so the two cannot drift.
def _min_trades() -> int:
    from backend.src.services.backtest.split import MIN_TRADES_PER_SIDE
    return MIN_TRADES_PER_SIDE


MIN_TRADES = _min_trades()

_BOUNCE   = "bounce"
_BREAKOUT = "breakout"
_ENGINES  = (_BOUNCE, _BREAKOUT)


@dataclass(frozen=True)
class Cell:
    key:            str
    trades:         int
    total_pnl:      float
    win_rate:       float
    blocked_today:  bool
    verdict:        str


@dataclass(frozen=True)
class DriftReport:
    dimension:      str
    engine:         str
    constants_from: tuple[str, str] = CONSTANTS_FITTED_TO
    window_from:    float = 0.0
    window_to:      float = 0.0
    cells:          list[Cell] = field(default_factory=list)
    # Records the dimension could not place, counted rather than dropped.
    skipped:        int = 0
    note:           str = ""

    @property
    def contradictions(self) -> list[Cell]:
        return [c for c in self.cells if c.verdict == CONTRADICTS]

    @property
    def unfalsifiable(self) -> list[Cell]:
        """Cells the constants have sealed off from ever producing evidence."""
        return [c for c in self.cells if c.verdict == NO_EVIDENCE_BLOCKED]


# ── Verdict ──────────────────────────────────────────────────────────────────

def _verdict(trades: int, total_pnl: float, blocked: bool, floor: int) -> str:
    if trades < floor:
        return NO_EVIDENCE_BLOCKED if blocked else INSUFFICIENT
    if blocked and total_pnl > 0:
        return CONTRADICTS      # we forbid a cell that is making money
    if not blocked and total_pnl < 0:
        return CONTRADICTS      # we allow a cell that is losing money
    return AGREES


def _cell(key: str, recs: list[dict], blocked: bool, floor: int) -> Cell:
    n     = len(recs)
    total = sum(float(r.get("net_pnl_dollars") or 0.0) for r in recs)
    wins  = sum(1 for r in recs if float(r.get("net_pnl_dollars") or 0.0) > 0)
    return Cell(
        key=key, trades=n, total_pnl=round(total, 2),
        win_rate=round(100.0 * wins / n, 2) if n else 0.0,
        blocked_today=blocked,
        verdict=_verdict(n, total, blocked, floor),
    )


def _window(records: list[dict]) -> tuple[float, float]:
    ts = [float(r["created_at"]) for r in records if r.get("created_at") is not None]
    return (min(ts), max(ts)) if ts else (0.0, 0.0)


def _check_engine(engine: str) -> None:
    if engine not in _ENGINES:
        raise ValueError(f"unknown engine {engine!r}; expected one of {_ENGINES}")


def _empty_note(records: list[dict]) -> str:
    if records:
        return ""
    return ("no closed signals in the window; this report says nothing about "
            "the constants, in either direction")


# ── Hour of day ──────────────────────────────────────────────────────────────

def hour_drift(records: list[dict], engine: str,
               min_trades: int | None = None) -> DriftReport:
    """Per-UTC-hour expectancy against `*_BLOCKED_HOURS_UTC`.

    Every hour appears, including the ones with nothing in them: an hour
    missing from the table is an hour nobody looks at.
    """
    _check_engine(engine)
    floor   = _min_trades() if min_trades is None else min_trades
    blocked = (rg.BOUNCE_BLOCKED_HOURS_UTC if engine == _BOUNCE
               else rg.BREAKOUT_BLOCKED_HOURS_UTC)

    buckets: dict[int, list[dict]] = {h: [] for h in range(24)}
    skipped = 0
    for r in records:
        ts = r.get("created_at")
        if ts is None:
            skipped += 1
            continue
        hour = datetime.fromtimestamp(float(ts), tz=timezone.utc).hour
        buckets[hour].append(r)

    lo, hi = _window(records)
    return DriftReport(
        dimension="hour", engine=engine, window_from=lo, window_to=hi,
        skipped=skipped, note=_empty_note(records),
        cells=[_cell(f"{h:02d}", buckets[h], h in blocked, floor)
               for h in range(24)],
    )


# ── Level type ───────────────────────────────────────────────────────────────

def level_drift(records: list[dict], engine: str,
                min_trades: int | None = None) -> DriftReport:
    """Per-level-type expectancy against `BOUNCE_BLOCKED_ENTRY_LEVELS` /
    `BREAKOUT_BLOCKED_LEVELS`.

    Unlike hours, the set of level types is open: the constants name the ones
    that were losing in one window, and a type introduced since must still be
    reported or it is invisible.
    """
    _check_engine(engine)
    floor   = _min_trades() if min_trades is None else min_trades
    blocked = (rg.BOUNCE_BLOCKED_ENTRY_LEVELS if engine == _BOUNCE
               else rg.BREAKOUT_BLOCKED_LEVELS)

    buckets: dict[str, list[dict]] = {k: [] for k in blocked}
    skipped = 0
    for r in records:
        lt = r.get("level_type")
        if not lt:
            skipped += 1
            continue
        buckets.setdefault(str(lt), []).append(r)

    lo, hi = _window(records)
    return DriftReport(
        dimension="level_type", engine=engine, window_from=lo, window_to=hi,
        skipped=skipped, note=_empty_note(records),
        cells=[_cell(k, buckets[k], k in blocked, floor)
               for k in sorted(buckets)],
    )
