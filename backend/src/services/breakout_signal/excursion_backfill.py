"""Reconstruct how far a closed breakout trade actually travelled.

The breakout engine has never measured excursion. `bo_signals` carried no
`mfe_pts`/`mae_pts` until 2026-09-16, so there has never been a reach
distribution for it and never an evidence base for its targets: `tp1_mult`
has sat at 1.0 since the engine was written, `rr_tp1` is 1.0 on every one of
its 122 closed signals, and at the realised payoff of 1.30 that needs a 43.5%
win rate against an actual 36.1%.

The Reversal Engine hit the same wall on 2026-09-11 and answered it the same
way (`reversal_engine/excursion_backfill.py`): the broker still holds the
tick history those trades walked through, so the data does not have to
accumulate forward for months. This is that mechanism for the other engine,
against the same `market/price_path` reconstruction, so the two engines'
numbers mean the same thing and can be compared.

**This writes one column pair and changes no decision.** It places nothing,
closes nothing, modifies no order, and never touches a row that already
carries an excursion.

Three filters, each keeping a different fiction out of the fit:

  * `live_exec_status='executed'` -- a virtual signal's path is whatever the
    engine imagined, and pooling those with real fills produces a
    distribution describing a population that never traded.
  * `status='closed'` -- an open trade's path is not finished.
  * `mfe_pts IS NULL` -- nothing already measured is overwritten.

Two honest limits, both worth stating before the numbers get used:

  * The reconstruction is capped at the trade's real `close_time`. What price
    did after an early exit is not part of that trade's path, and counting it
    would measure a trade nobody held.
  * It depends on the broker still retaining ticks that far back.
    `reversal_engine.excursion_backfill.probe_tick_history` answers that in
    one call and is not duplicated here -- a run that comes back mostly
    `no_coverage` should be read against it, because "the broker does not
    keep ticks that far back" and "the market was closed" produce the same
    zero.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field

from backend.src.services.breakout_signal import measure_repo as bdb
from backend.src.services.market import price_path as pp

_log = logging.getLogger("breakout_signal")

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
    errors: list = field(default_factory=list)

    def summary(self) -> str:
        return (f"{self.measured} measured, {self.no_coverage} with no tick "
                f"coverage, {self.skipped_no_window} unusable, "
                f"{self.failed} failed, of {self.considered} considered")


def _window(row: dict):
    """`(entry, from_ts, to_ts)` or None when the row cannot be measured.

    The fill price is `trigger_price`. Falling back to `entry_mid` was
    considered and rejected for the same reason the reversal engine rejected
    its entry zone: that is where the signal asked to be filled, not where it
    was, and the gap between the two is one of the things worth measuring.
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


def _chunks(from_ts: float, to_ts: float) -> list:
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
    """Measure every executed, closed breakout signal with no excursion yet."""
    report = BackfillReport()
    rows = bdb.signals_awaiting_excursion_backfill(limit)
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
            # A zero excursion and an unmeasurable one are different facts.
            # Writing 0.0 would put a trade that never moved and a trade
            # nobody has data for into the same bucket of the fit.
            report.no_coverage += 1
            continue

        bdb.record_backfilled_excursion(int(row["id"]), *measured)
        report.measured += 1

    _log.info("[BO-Engine] excursion backfill: %s", report.summary())
    return report


async def run(limit: int = 500) -> dict:
    """The whole operation, for one controller line to forward to.

    The bridge comes from the running engine rather than the caller: the
    backfill has to read the history of the connection the engine actually
    traded on, and a controller that had to find one would be reaching past
    its layer to do it.
    """
    from backend.src.services.breakout_signal import (
        breakout_signal_service as _svc)
    engine = _svc.get_instance()
    bridge = getattr(engine, "_bridge", None) if engine else None
    if bridge is None:
        return {"error": ("The breakout engine is not running, so there is no "
                          "broker connection to read history through.")}
    report = await backfill(bridge, limit)
    return {**report.__dict__, "summary": report.summary(),
            "coverage": bdb.excursion_coverage()}
