"""Reconstruct how far a closed trade actually travelled, from broker ticks.

Section 1.2 of `docs/todo/reversal-engine/200`. Items
[020](../../../../docs/todo/reversal-engine/020-losses-exceed-the-stop.md) and
[030](../../../../docs/todo/reversal-engine/030-wins-are-cut-at-two-thirds-of-a-r.md)
are both blocked on excursion data accumulating forward at roughly nine
signals a day, which put them in October. They need not be: the broker holds
the tick history those trades walked through, and `bridge.get_ticks_range`
has been wired from `mt5_bridge._get_ticks_range` through `/ticks` to
`runtime.get_ticks_range` since before either item was raised.

**This writes one column pair and changes no decision.** It places nothing,
closes nothing, and never touches a row that already carries an excursion.

Two honest limits, both worth stating before the numbers get used:

  * The reconstruction is capped at the trade's real `close_time`. What price
    did after an early exit is not part of that trade's path, and counting it
    would measure a trade nobody held.
  * It depends on the broker still retaining ticks that far back. `probe_tick_
    history` answers that in one call; run it before assuming a run that
    reported mostly `no_coverage` means the market was quiet.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend.src.services.market import price_path as pp
from backend.src.services.reversal_engine import measure_repo

_log = logging.getLogger("reversal_engine")

# The bridge refuses a span wider than this and returns None rather than
# raising, so an unchunked request for a multi-day trade would silently
# record no coverage at all. Mirrors `mt5_bridge._MAX_TICKS_RANGE_SEC`.
MAX_WINDOW_S = 86_400.0


@dataclass
class BackfillReport:
    considered: int = 0
    measured: int = 0
    no_coverage: int = 0
    skipped_no_window: int = 0
    failed: int = 0
    errors: list[str] = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.measured} measured, {self.no_coverage} with no tick "
                f"coverage, {self.skipped_no_window} unusable, "
                f"{self.failed} failed, of {self.considered} considered")


def _window(row: dict) -> tuple[float, float, float] | None:
    """`(entry, from_ts, to_ts)` or None when the row cannot be measured.

    The fill price is `trigger_price`. Falling back to the middle of the
    stated entry zone was considered and rejected: the zone is where the
    signal asked to be filled, not where it was, and the gap between the two
    is itself one of the candidate explanations for item 020.
    """
    try:
        entry = float(row.get("trigger_price") or 0.0)
        t0 = float(row.get("trigger_time") or 0.0)
        t1 = float(row.get("close_time") or 0.0)
    except (TypeError, ValueError):
        return None
    if entry <= 0.0 or t0 <= 0.0 or t1 <= t0:
        return None
    return entry, t0, t1


def _chunks(from_ts: float, to_ts: float) -> list[tuple[float, float]]:
    out = []
    cursor = from_ts
    while cursor < to_ts:
        end = min(cursor + MAX_WINDOW_S, to_ts)
        out.append((cursor, end))
        cursor = end
    return out


async def _fetch_path(bridge, direction: str, from_ts: float,
                      to_ts: float) -> list:
    path: list = []
    for a, b in _chunks(from_ts, to_ts):
        ticks = await bridge.get_ticks_range(a, b)
        path.extend(pp.build_tick_path(ticks or [], direction))
    path.sort(key=lambda p: p[0])
    return path


async def backfill(bridge, limit: int = 500) -> BackfillReport:
    """Measure every executed, closed signal that has no excursion yet."""
    report = BackfillReport()
    rows = measure_repo.signals_awaiting_excursion_backfill(limit)
    report.considered = len(rows)

    for row in rows:
        win = _window(row)
        if win is None:
            report.skipped_no_window += 1
            continue
        entry, t0, t1 = win
        direction = str(row.get("direction") or "BUY")
        try:
            path = await _fetch_path(bridge, direction, t0, t1)
        except Exception as e:                      # noqa: BLE001 - reported, not swallowed
            report.failed += 1
            report.errors.append(f"signal {row.get('id')}: {e}")
            continue

        measured = pp.excursion(path, entry, direction)
        if measured is None:
            report.no_coverage += 1
            continue

        measure_repo.record_backfilled_excursion(int(row["id"]), *measured)
        report.measured += 1

    _log.info("[RE-Engine] excursion backfill: %s", report.summary())
    return report


async def probe_tick_history(bridge, now: float,
                             days_back: tuple[int, ...] = (1, 7, 30, 90, 180)) -> dict:
    """How far back this broker actually serves ticks, as `{days: n_ticks}`.

    One call per probe point, an hour wide each. Run this BEFORE reading
    anything into a backfill that came back mostly empty: "the broker does not
    keep ticks that far back" and "the market was closed" produce the same
    zero, and only one of them means the data is unavailable.
    """
    out: dict[int, int] = {}
    for d in days_back:
        start = now - d * 86_400.0
        try:
            ticks = await bridge.get_ticks_range(start, start + 3_600.0)
            out[d] = len(ticks or [])
        except Exception:                            # noqa: BLE001
            out[d] = -1
    return out
