"""Capture what the market looked like the moment a reference-channel
signal arrived (2026-08-04).

WHY
---
We copy Gold Diggers VIP and GOLD DIGGERS INSTITUTIONAL without knowing
what they are reading. Their chart screenshots show FVG/iFVG, and their
messages show a two-stage pattern (VIP fires a bare market call, then sends
the zone and levels ~40s later) -- but "why this level, why now, why market
rather than limit" is currently guesswork.

So: log the evidence. One row per signal EVENT with the full indicator
picture across M1/M5/M15, and let a week of real data answer it, rather
than reasoning from two screenshots.

DESIGN NOTES
------------
Poll-driven rather than hooked into the parser. There are seven separate
INSERT sites for vantage_tg_signals across the scan/instant-entry/backfill
paths; hooking each would be invasive, easy to miss one, and risks breaking
signal processing for a research feature. Polling for un-snapshotted rows
catches every path including ones added later, and cannot break trading.

The cost is a few seconds of capture lag. That is recorded explicitly
(capture_lag_s) rather than hidden, and it barely matters for the
candle-derived indicators, which only change on bar close. It DOES matter
for bid/ask, so those are labelled as at-capture, not at-signal.

Nothing here is on the trading path and every failure is swallowed: a
research log must never be able to stop a trade.

WHERE THE ROWS GO (changed 2026-08-06)
--------------------------------------
Into reversal_engine.db via pro_corpus.py, not the core database. The core
db is per-environment, so the original design quietly split this corpus in
two the moment the app switched between demo and live. See pro_corpus.py.
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Optional

from backend.src.db import database as db_module
from backend.src.services.positions import repo as positions_repo
from backend.src.services.positions.core_indicators import ema_last, rsi_last

log = logging.getLogger(__name__)

# The two channels this study is about.
WATCHED = ("Gold Diggers VIP", "GOLD DIGGERS INSTITUTIONAL")

_TFS = ("M1", "M5", "M15")
_CANDLES_PER_TF = 120     # enough for EMA50 + RSI14 + ATR14 to be settled


def _stage_for(row: dict) -> str:
    """Which moment of a (possibly two-stage) signal this row represents.

    VIP's bare market call arrives with a direction but no levels; the
    follow-up fills them in. Distinguishing them is the whole point -- see
    this module's docstring.
    """
    has_levels = bool(row.get("entry_low") or row.get("entry_high") or row.get("stop_loss"))
    status = (row.get("status") or "").lower()
    if not has_levels:
        return "market_call"
    if "followup" in status or "instant" in status:
        return "levels"
    return "complete"


async def _tf_indicators(bridge: Any, tf: str) -> Optional[dict]:
    try:
        candles = await bridge.get_candles(tf, _CANDLES_PER_TF)
    except Exception:
        return None
    if not candles or len(candles) < 30:
        return None
    closes = [float(c.get("close", 0) or 0) for c in candles]
    vols = [float(c.get("volume", 0) or 0) for c in candles]
    try:
        from backend.src.services.dpm.engine import compute_atr, compute_adx
        atr = compute_atr(candles)
        adx = compute_adx(candles)
    except Exception:
        atr = adx = None

    last_v = vols[-1] if vols else 0.0
    avg_v = sum(vols[-20:]) / max(len(vols[-20:]), 1)
    ema9, ema21, ema50 = (ema_last(closes, 9), ema_last(closes, 21), ema_last(closes, 50))
    px = closes[-1]
    return {
        "close": round(px, 2),
        "ema9": ema9, "ema21": ema21, "ema50": ema50,
        # Stacking is the readable part of three EMAs -- record the verdict
        # as well as the values so later analysis needn't re-derive it.
        "ema_stack": ("bull" if ema9 and ema21 and ema50 and ema9 > ema21 > ema50
                      else "bear" if ema9 and ema21 and ema50 and ema9 < ema21 < ema50
                      else "mixed"),
        "px_vs_ema21": round(px - ema21, 2) if ema21 else None,
        "rsi14": rsi_last(closes),
        "atr14": round(atr, 3) if atr else None,
        "adx14": round(adx, 2) if adx else None,
        "volume": last_v,
        "volume_avg20": round(avg_v, 1),
        # >1 means this bar traded heavier than recent norm.
        "volume_ratio": round(last_v / avg_v, 3) if avg_v else None,
    }


async def capture_snapshot(bridge: Any, row: dict,
                           stage_override: Optional[str] = None) -> bool:
    """Snapshot the market for one vantage_tg_signals row. Returns True if a
    row was written."""
    stage = stage_override or _stage_for(row)
    now = time.time()
    signal_ts = float(row.get("parsed_at") or 0)

    try:
        tick = await bridge.get_tick()
    except Exception:
        tick = None
    bid = float(getattr(tick, "bid", 0) or 0)
    ask = float(getattr(tick, "ask", 0) or 0)
    price = (bid + ask) / 2 if (bid and ask) else (bid or ask or 0.0)

    inds = {}
    for tf in _TFS:
        got = await _tf_indicators(bridge, tf)
        if got:
            inds[tf] = got

    # FVG context measured against what they actually asked for (the zone
    # mid) when stated, else against live price for a bare market call.
    el, eh = row.get("entry_low"), row.get("entry_high")
    entry_mid = ((float(el) + float(eh)) / 2) if (el and eh) else price
    fvg = {}
    try:
        from backend.src.services.reversal_engine.ict_patterns import fvg_context, detect_fvgs
        m15 = await bridge.get_candles("M15", 300)
        if m15:
            _atr = (inds.get("M15") or {}).get("atr14") or 5.0
            fvg = fvg_context(m15, entry_mid, row.get("direction") or "BUY", _atr)
            fvg["n_open_gaps"] = sum(
                1 for f in detect_fvgs(m15) if not f["filled"] and not f["inverted"])
    except Exception:
        fvg = {}

    # Same session/regime definitions the Reversal Engine itself records, so
    # this log is directly comparable with re_signals rather than using a
    # second, subtly different notion of "which session is it".
    session = ""
    regime = None
    try:
        from backend.src.services.reversal_engine.level_detector import get_session
        session = get_session(time.gmtime(now).tm_hour)
    except Exception:
        pass
    try:
        _m15 = inds.get("M15") or {}
        if _m15.get("adx14") is not None and _m15.get("atr14") is not None:
            regime = db_module.get_regime_score(_m15["adx14"], _m15["atr14"])
    except Exception:
        pass

    inside = None
    if el and eh:
        inside = 1 if float(el) <= price <= float(eh) else 0

    # Written to the Reversal Engine's own database, which is one shared file
    # across demo and live -- NOT the per-environment core db this module used
    # to write to. See pro_corpus.py's docstring: a corpus that splits when
    # the account environment changes is a corpus the model silently loses.
    record = {
        "tg_message_id": row.get("tg_message_id"), "stage": stage,
        "group_name": row.get("group_name"), "direction": row.get("direction"),
        "signal_ts": signal_ts, "captured_at": now,
        "capture_lag_s": round(now - signal_ts, 2) if signal_ts else None,
        "entry_low": el, "entry_high": eh,
        "stop_loss": row.get("stop_loss"), "tp1": row.get("tp1"),
        "bid": bid, "ask": ask,
        "spread_points": float(getattr(tick, "spread_points", 0) or 0),
        "price": price,
        "dist_to_entry_mid": round(price - entry_mid, 2) if (el and eh) else None,
        "price_inside_zone": inside, "session": session, "regime_score": regime,
        "indicators_json": json.dumps(inds), "fvg_json": json.dumps(fvg),
        "raw_text": (row.get("raw_text") or "")[:2000],
    }

    def _write():
        from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
        return pro_corpus.insert(record)

    if not await db_module.to_db_thread(_write):
        return False

    # One refit per captured signal -- the Learn From Pro Signals toggle. Off
    # by default, and a no-op when the corpus hasn't actually grown.
    if stage != "background":
        try:
            from backend.src.services.reversal_engine import ml_engine as _re_ml
            if _re_ml.learning_from_ref_enabled():
                from backend.src.services.reversal_engine import pro_model
                await db_module.to_db_thread(pro_model.on_new_signal)
        except Exception as exc:
            log.debug("[SigSnap] pro-model refit skipped: %s", exc)
    log.info("[SigSnap] captured %s stage=%s %s lag=%.1fs",
             row.get("group_name"), stage, row.get("direction"),
             (now - signal_ts) if signal_ts else -1)
    return True


async def capture_background_snapshot(bridge: Any) -> int:
    """Snapshot the market on a timer, with NO signal attached.

    Added 2026-08-05. Without these, the only samples we hold are moments the
    reference channels chose to fire -- all positives. A model given only
    positives can describe what their entries look like ("RSI ~68, ADX ~47,
    FVG confluence") but cannot learn what DISTINGUISHES a signal moment from
    an ordinary one, because those same readings may occur constantly without
    them acting. These rows are the negatives that make the difference
    learnable.

    Two rows per tick, one per direction: FVG context is direction-relative
    (a BUY wants an aligned bullish gap below), so a single direction-less
    sample could not be compared against a directional signal.
    """
    n = 0
    for direction in ("BUY", "SELL"):
        row = {
            "tg_message_id": f"bg-{int(time.time())}-{direction}",
            "group_name": "_BACKGROUND",
            "direction": direction,
            "parsed_at": time.time(),
            "status": "background",
            "entry_low": None, "entry_high": None,
            "stop_loss": None, "tp1": None, "raw_text": "",
        }
        try:
            if await capture_snapshot(bridge, row, stage_override="background"):
                n += 1
        except Exception as e:
            log.debug("[SigSnap] background capture failed: %s", e)
    return n


async def capture_pending_snapshots(bridge: Any, max_age_s: float = 900.0) -> int:
    """Snapshot every watched-channel signal that has no row for its current
    stage yet. Returns how many were captured.

    max_age_s stops a backlog (or a first run) from snapshotting hours-old
    signals against today's market, which would be worse than no data --
    the whole value here is that the reading is contemporaneous.
    """
    def _fetch():
        return positions_repo.fetch_recent_signals_for_groups(
            list(WATCHED), time.time() - max_age_s)

    def _already(msg_id, stage):
        from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
        return pro_corpus.exists(msg_id, stage)

    try:
        rows = await db_module.to_db_thread(_fetch)
    except Exception as e:
        log.debug("[SigSnap] fetch failed: %s", e)
        return 0

    n = 0
    for row in rows:
        stage = _stage_for(row)
        try:
            if await db_module.to_db_thread(_already, row.get("tg_message_id"), stage):
                continue
            if await capture_snapshot(bridge, row):
                n += 1
        except Exception as e:
            # Never let a research log interfere with anything.
            log.debug("[SigSnap] capture failed for %s: %s", row.get("tg_message_id"), e)
    return n


@dataclass
class SnapshotState:
    """What one snapshot tick remembers from the last. Held by the caller --
    runtime.py creates one before its loop -- so nothing persists at module
    level between runs."""
    last_background: float = 0.0
    last_pro_resolve: float = 0.0


BACKGROUND_INTERVAL_S = 900.0
PRO_RESOLVE_INTERVAL_S = 60.0


async def run_snapshot_cycle(
    state: "SnapshotState", bridge: Any, now: Optional[float] = None, *,
    capture=None, background=None, resolve=None,
) -> None:
    """One tick of the signal-snapshot research log.

    Three cadences share it. The per-signal capture runs every tick, which the
    caller paces at 5s so the candle-derived indicators stay effectively
    contemporaneous with the signal. Background negatives run every 15 minutes
    (see capture_background_snapshot for why the study is unusable without
    them). The pro-outcome resolve runs every 60s; it walks from a cursor, so a
    slower cadence costs latency and nothing else.

    Each of the three is caught separately, and a failing slow cadence still
    advances its own clock. Both matter: this is a research log, and the reason
    it polls rather than hooking the parser is that it must never be able to
    break signal processing -- so it must not be able to break itself into a
    tight retry either.

    The three actions are injectable. runtime.py passes its own module-level
    aliases, which is the seam tests/runtime/test_background_loops.py patches
    to drive the loop -- calling the module functions directly here would take
    a reference that patch never reaches.
    """
    now = time.time() if now is None else now
    _capture = capture or capture_pending_snapshots
    _background = background or capture_background_snapshot

    try:
        await _capture(bridge)
    except Exception:
        log.debug("Signal snapshot capture failed", exc_info=True)

    if now - state.last_background > BACKGROUND_INTERVAL_S:
        state.last_background = now
        try:
            await _background(bridge)
        except Exception:
            log.debug("Background snapshot failed", exc_info=True)

    if now - state.last_pro_resolve > PRO_RESOLVE_INTERVAL_S:
        state.last_pro_resolve = now
        try:
            if resolve is not None:
                await resolve(bridge)
            else:
                from backend.src.services.reversal_engine import pro_outcome as _pro_out
                await _pro_out.resolve_pending(bridge)
        except Exception:
            log.debug("Pro outcome resolve failed", exc_info=True)
