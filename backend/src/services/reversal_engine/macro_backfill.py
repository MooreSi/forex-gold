"""Give the five macro features real values in the training set.

Section 4.4 of `docs/todo/reversal-engine/200`.

`data-inspect/003` measured the problem: v9 widened the feature vector from
33 to 38 and back-filled the five macro slots with `_FEATURE_NEUTRAL` for
every row written before the changeover. Of roughly 4,050 training rows only
~175 carry real macro values, so **five of the model's 38 inputs are a
constant for 96% of what it learns from.** A constant input cannot help and
can hurt through variance.

The fix is not a sixth series. It is giving the five that exist real values,
from the same source they come from live (`yfinance`, which serves hourly
history) and with the same arithmetic (`market_context`'s own scales: 0.5%
an hour for DXY, 0.2% for TIP). Reconstructing them differently would make
the back-filled rows and the live rows two different features sharing one
name, which is worse than the neutrals.

**It is a dry run by default.** Rewriting stored training vectors changes
what the live ML gate learns at its next retrain, so it is a behaviour
change even though it places nothing. `apply=True` is a decision somebody
makes, not a default.
"""
from __future__ import annotations

import json
import logging
from typing import Callable, Optional, Sequence

from backend.src.services.reversal_engine.re_macro import (
    MACRO_FEATURE_NAMES, MACRO_NEUTRAL, _normalise)
from backend.src.services.test_signal.market_context import _NEUTRAL

log = logging.getLogger("reversal_engine")

# Raw (un-normalised) neutrals, from market_context's own table rather than
# restated, so the two cannot drift.
NEUTRAL_RAW: dict = dict(_NEUTRAL)

# yfinance symbols, matching market_context.get_context exactly.
SYMBOLS = {
    "dxy_momentum": "DX-Y.NYB",
    "us10y_level": "^TNX",
    "vix_level": "^VIX",
    "gvz_level": "^GVZ",
    "tip_momentum": "TIP",
}

# Percent move in one hour that maps to +-1.0, per market_context.
MOMENTUM_SCALE = {"dxy_momentum": 0.5, "tip_momentum": 0.2}

FULL_WIDTH = 38
MACRO_START = FULL_WIDTH - len(MACRO_FEATURE_NAMES)


def _closes_at(series: Sequence[tuple[float, float]],
               ts: float) -> tuple[Optional[float], Optional[float]]:
    """The last two closes at or before `ts`.

    Never a later one. A training row that saw tomorrow's VIX is the purest
    form of the leakage purged cross-validation exists to prevent, and it
    would be invisible: the vector would simply look unusually predictive.
    """
    prior = [c for t, c in series if t <= ts]
    if not prior:
        return None, None
    if len(prior) == 1:
        return prior[-1], None
    return prior[-1], prior[-2]


def raw_at(ts: float, series_by_symbol: dict) -> dict:
    """The five RAW macro readings at `ts`, neutral where unavailable.

    Raw rather than normalised because `re_macro._normalise` is the single
    definition of the scaling and this must not become a second one.
    """
    out: dict = {}
    for name in MACRO_FEATURE_NAMES:
        series = series_by_symbol.get(SYMBOLS[name]) or []
        last, prev = _closes_at(series, ts)
        if last is None:
            out[name] = NEUTRAL_RAW[name]
            continue
        scale = MOMENTUM_SCALE.get(name)
        if scale is None:
            out[name] = float(last)
            continue
        if prev in (None, 0):
            out[name] = NEUTRAL_RAW[name]
            continue
        pct = (last - prev) / prev * 100.0
        out[name] = round(max(-1.0, min(1.0, pct / scale)), 4)
    return out


def normalised_at(ts: float, series_by_symbol: dict) -> list[float]:
    raw = raw_at(ts, series_by_symbol)
    return [_normalise(name, float(raw[name])) for name in MACRO_FEATURE_NAMES]


def needs_backfill(vector: Sequence[float]) -> bool:
    """True when this vector's macro slots are all still the neutral.

    A short pre-v9 vector has no macro slots at all and returns False:
    padding it here rather than in `ml_engine/_training_data` would apply
    the widening twice.
    """
    if len(vector) < FULL_WIDTH:
        return False
    slots = list(vector[MACRO_START:FULL_WIDTH])
    return all(abs(float(v) - MACRO_NEUTRAL[name]) < 1e-9
               for v, name in zip(slots, MACRO_FEATURE_NAMES))


def backfill(rows, series_by_symbol: dict,
             write_fn: Callable, apply: bool = False) -> dict:
    """Recompute the macro slots of every row that still carries neutrals.

    `write_fn` receives `(signal_id, vector)` and is called only when
    `apply` is True. A row whose recomputation produces the same neutrals
    it started with is counted under `no_data` and NOT written: marking it
    repaired when nothing was repaired is how a second run reports there is
    nothing left to do.
    """
    report = {"considered": 0, "would_update": 0, "updated": 0,
              "no_data": 0, "unreadable": 0, "skipped": 0,
              "dry_run": not apply}

    for row in rows or ():
        report["considered"] += 1
        try:
            vector = json.loads(row.get("ml_features_json") or "")
        except (ValueError, TypeError):
            report["unreadable"] += 1
            continue
        if not isinstance(vector, list) or not needs_backfill(vector):
            report["skipped"] += 1
            continue

        fresh = normalised_at(float(row.get("created_at") or 0.0),
                              series_by_symbol)
        if all(abs(v - MACRO_NEUTRAL[n]) < 1e-9
               for v, n in zip(fresh, MACRO_FEATURE_NAMES)):
            report["no_data"] += 1
            continue

        updated = list(vector)
        updated[MACRO_START:FULL_WIDTH] = fresh
        if apply:
            write_fn((int(row["id"]), updated))
            report["updated"] += 1
        else:
            report["would_update"] += 1

    log.info("[RE-Macro] backfill: %s", report)
    return report


def fetch_history(start_ts: float, end_ts: float) -> dict:
    """Hourly closes per symbol from yfinance, or {} if it is unavailable.

    Separated from `backfill` so the reconstruction is testable without a
    network, which is the only way the arithmetic above can be pinned
    against `market_context`'s.
    """
    try:
        import yfinance as yf
    except ImportError:
        log.info("[RE-Macro] yfinance unavailable; cannot backfill")
        return {}

    import datetime as _dt
    out: dict = {}
    start = _dt.datetime.fromtimestamp(start_ts, tz=_dt.timezone.utc)
    end = _dt.datetime.fromtimestamp(end_ts, tz=_dt.timezone.utc)
    for symbol in set(SYMBOLS.values()):
        try:
            df = yf.download(symbol, start=start, end=end, interval="1h",
                             progress=False, auto_adjust=False)
            if df is None or df.empty:
                continue
            out[symbol] = [(idx.timestamp(), float(row["Close"]))
                           for idx, row in df.iterrows()]
        except Exception as e:                    # noqa: BLE001
            log.debug("[RE-Macro] %s history unavailable: %s", symbol, e)
    return out


def run(apply: bool = False) -> dict:
    """Fetch the history the stored vectors span, then repair them.

    The orchestration lives here rather than in the controller: a
    controller names an operation and forwards it to one service, and this
    reads a repo, reaches the network and writes rows.
    """
    from backend.src.services.reversal_engine import measure_repo

    rows = measure_repo.training_vectors()
    if not rows:
        return {"considered": 0, "dry_run": not apply}
    oldest = min(float(r.get("created_at") or 0.0) for r in rows)
    newest = max(float(r.get("created_at") or 0.0) for r in rows)
    series = fetch_history(oldest - 7200.0, newest + 3600.0)
    return backfill(
        rows, series,
        write_fn=lambda pair: measure_repo.write_training_vector(
            pair[0], json.dumps(pair[1])),
        apply=apply)
