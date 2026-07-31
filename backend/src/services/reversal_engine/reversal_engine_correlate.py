"""REF-signal correlation tracking for Reversal Engine -- extracted verbatim (no
logic changes) from engine.py's _check_correlation/_classify_ref_level/
_ref_cadence_stats as part of task 040. See
docs/todo/refactor/backend-foundation/040-*.md.

_CorrelationMixin is composed into ReversalEngine (reversal_engine_service.py).

Note (carried over from the 020 characterization scope note): this module
reaches into the CORE engine's database directly via a raw sqlite3
connection to read vantage_tg_signals -- a real cross-engine coupling this
extraction preserves as-is rather than silently changing. Worth a future
pack once the database-consolidation question (QUESTIONS.md #6) is
revisited.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

from backend.src.services.reversal_engine import reversal_engine_repo as re_db

_log = logging.getLogger("reversal_engine")

# Correlation semantics: our signal PREDICTS a REF signal when the REF entry
# arrives at the same level while our pending signal is still alive. We create
# zone signals as price APPROACHES a level; REF posts when price ARRIVES -- so
# legitimate matches routinely lead by 10-30 minutes (near-miss data: 498 of
# 511 direction+price matches failed only on the old symmetric +/-300s window,
# with avg lead -836s and avg price distance 1.1pts). The window is therefore
# asymmetric: we may lead by up to the signal lifetime, or lag by 5 minutes
# (Telegram delivery latency).
_SIGNAL_MAX_AGE_S    = 7200    # 2-hour pending expiry (must match reversal_engine_service._SIGNAL_MAX_AGE_S)
_REF_CORR_LEAD_MAX_S = _SIGNAL_MAX_AGE_S  # we fired first: up to 2h (signal lifetime)
_REF_CORR_LAG_MAX_S  = 300     # REF fired first: 5 min grace (delivery latency)
_REF_CORR_PRICE_DELTA = 3.0    # pts between entry-zone midpoints (zones are 3-4pts wide)

# Stable Telegram group IDs -- never change on a channel rename, unlike
# group_name (see telegram_research.py's identical constants). Matching on
# these instead of a hardcoded name string is what makes this module
# immune to the exact bug found 2026-07-24: GD2's channel was renamed on
# Telegram's side ("GOLD DIGGERS 2.0 ⚡️" -> "GOLD DIGGERS INSTITUTIONAL",
# same group_id 2616846888) and every hardcoded name-string comparison in
# this file silently stopped matching any real signal recorded after the
# rename, since vantage_tg_signals isn't part of core_db_channel.
# sync_channel_rename's cascade (only channel_parser_config/
# channel_performance/etc. are).
_REF_GROUP_ID = "1608388054"
_GD2_GROUP_ID = "2616846888"


class _CorrelationMixin:
    async def _check_correlation(self) -> None:
        """
        Scan recent real signals from BOTH covered channels (Gold Diggers
        REF and Gold Diggers 2.0 / Institutional) in vantage_tg_signals and
        match them against our recent RE signals.

        A match is:
          - Same direction
          - Entry zone midpoints within _REF_CORR_PRICE_DELTA points
          - Time difference within the asymmetric lead/lag window
          - Same channel: a RE signal only matches real signals from the
            channel it was modelled on (source_channel), never cross-channel.
        """
        _covered_group_ids = (_REF_GROUP_ID, _GD2_GROUP_ID)
        # Recent real signals from either channel (last 4 hours) -- used
        # for the actual matching window.
        cutoff = time.time() - 14400

        def _fetch_ref_data():
            import sqlite3
            from backend.src.config import get as cfg_get
            db_path = cfg_get("db_path", "")
            if not db_path:
                return None
            con = sqlite3.connect(db_path, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                _ph = ",".join("?" for _ in _covered_group_ids)
                ref_rows = con.execute(f"""
                    SELECT id, group_name, direction, entry_low, entry_high, parsed_at, status
                    FROM vantage_tg_signals
                    WHERE group_id IN ({_ph})
                    AND direction IN ('BUY','SELL')
                    AND parsed_at > ?
                    ORDER BY parsed_at DESC
                    LIMIT 100
                """, (*_covered_group_ids, cutoff)).fetchall()
                # True count of real signals received so far *today* (UTC), both
                # channels combined -- distinct from ref_rows above, which is only
                # a 4h rolling window used for matching.
                day_start = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()
                ref_today_count = con.execute(f"""
                    SELECT COUNT(*) FROM vantage_tg_signals
                    WHERE group_id IN ({_ph})
                    AND direction IN ('BUY','SELL')
                    AND parsed_at >= ?
                """, (*_covered_group_ids, day_start)).fetchone()[0]
                return ref_rows, ref_today_count
            finally:
                con.close()

        try:
            # Offloaded to the DB worker thread -- see test_signal.engine's
            # _reconcile_live_pnl for why a raw sqlite3.connect() here is a
            # whole-app hazard, not just a slow-task-local one.
            from backend.src.db import database as _mdb
            _fetch_result = await _mdb.to_db_thread(_fetch_ref_data)
        except Exception as exc:
            _log.debug("[RE-Engine] REF fetch error: %s", exc)
            return
        if _fetch_result is None:
            return
        ref_rows, ref_today_count = _fetch_result

        from backend.src.services.channels import repo as _cdc

        # Build lookup of recent RE signals (pending/triggered, last 4h)
        re_recent = [
            s for s in re_db.get_all_signals(limit=50)
            if float(s.get("created_at", 0)) > cutoff
        ]

        today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        predicted = 0
        lead_times: list[float] = []

        for re_sig in re_recent:
            if re_sig.get("correlation_confirmed"):
                continue  # already matched

            re_ts      = float(re_sig.get("created_at", 0))
            re_mid     = (float(re_sig.get("entry_low", 0) or 0)
                           + float(re_sig.get("entry_high", 0) or 0)) / 2
            re_dir     = re_sig.get("direction", "")
            re_channel = re_sig.get("source_channel") or "Gold Diggers VIP"

            for ref in ref_rows:
                # Never cross-correlate a GD2-modelled signal against a real
                # REF message or vice versa -- each RE signal is only ever
                # trying to predict the one channel it was built from.
                # Canonicalised on both sides (not a raw string equality) so
                # this keeps matching correctly regardless of which literal
                # name either side happens to hold after a channel rename --
                # ref["group_name"] is whatever the live channel is called
                # right now (via the group_id-scoped fetch above), re_channel
                # may still be an older name if it was stamped before a
                # rename. See core_db_channel._canonical()/CANONICAL_CHANNELS.
                if _cdc._canonical(ref["group_name"]) != _cdc._canonical(re_channel):
                    continue

                ref_mid = ((float(ref["entry_low"] or 0) + float(ref["entry_high"] or 0)) / 2)
                if ref_mid <= 0:
                    continue

                # Use parsed_at (already a float unix timestamp) -- more reliable
                # than parsing message_ts text which can have format variations.
                ref_ts = float(ref["parsed_at"] or 0)
                if ref_ts <= 0:
                    continue

                time_delta = re_ts - ref_ts   # negative = we were first
                dist = abs(re_mid - ref_mid)
                # Asymmetric window: our signal may LEAD the REF post by its
                # whole pending lifetime (we predict the level before price
                # arrives) but only LAG it by the Telegram delivery grace.
                time_ok = -_REF_CORR_LEAD_MAX_S <= time_delta <= _REF_CORR_LAG_MAX_S
                matched = (ref["direction"] == re_dir
                           and dist <= _REF_CORR_PRICE_DELTA
                           and time_ok)

                # Near-miss: direction matched but price/time missed the window --
                # log these (within widened tolerance) so thresholds can be tuned
                # empirically against real data instead of guessed constants.
                if (not matched and ref["direction"] == re_dir
                        and dist <= _REF_CORR_PRICE_DELTA * 4
                        and -_REF_CORR_LEAD_MAX_S * 2 <= time_delta <= _REF_CORR_LAG_MAX_S * 4):
                    reason = []
                    if dist > _REF_CORR_PRICE_DELTA:
                        reason.append("price")
                    if not time_ok:
                        reason.append("time")
                    re_db.log_near_miss(
                        re_signal_id=re_sig["id"],
                        ref_signal_id=str(ref["id"]),
                        direction=re_dir,
                        time_delta_s=round(time_delta, 1),
                        distance_pts=round(dist, 2),
                        reason="+".join(reason) or "unknown",
                    )

                if matched:

                    re_db.update_correlation(
                        sig_id=re_sig["id"],
                        ref_signal_id=str(ref["id"]),
                        time_delta_s=round(time_delta, 1),
                        distance_pts=round(dist, 2),
                    )
                    re_sig["correlation_confirmed"] = 1  # keep in-memory snapshot in sync for corr_total below

                    if time_delta < 0:  # we fired first -- that's the goal
                        predicted += 1

                    # Signed lead time: negative = we led, positive = we lagged
                    lead_times.append(time_delta)

                    # Feed REF level type back to ML (pattern learning)
                    ref_level_type = self._classify_ref_level(ref_mid)
                    from backend.src.services.reversal_engine import ml_engine as re_ml
                    re_ml.record_ref_signal(ref_level_type)

                    break  # one REF match per RE signal

        # avg_lead_time_s is signed: negative = we're ahead of the reference channel on average
        avg_lead = sum(lead_times) / len(lead_times) if lead_times else None

        # Query actual daily counts from DB rather than from the rolling 4h window.
        # The 4h window ages out confirmed signals within the same day, causing
        # re_correlated to be overwritten to 0 once correlations are >4h old.
        corr_total = re_db.count_today_correlated()
        today_sent = re_db.count_today_signals()
        corr_rate  = corr_total / today_sent if today_sent else 0.0

        re_db.upsert_daily_correlation(
            today,
            ref_signals_sent=ref_today_count,
            ref_predicted=predicted,
            re_correlated=corr_total,
            avg_lead_time_s=avg_lead,
            correlation_rate=round(corr_rate, 3),
        )

    async def _ref_cadence_stats(self) -> tuple[float, int]:
        """(minutes_since_last_real_ref_signal, ref_signals_received_today).
        Feeds the ML cadence features so the model can learn the reference channel's posting
        rhythm (e.g. quiet for 3h+ -> higher chance one is "due") instead of
        relying purely on static level geometry."""
        def _fetch():
            import sqlite3
            from backend.src.config import get as cfg_get
            db_path = cfg_get("db_path", "")
            if not db_path:
                return None
            con = sqlite3.connect(db_path, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                last_row = con.execute("""
                    SELECT parsed_at FROM vantage_tg_signals
                    WHERE group_id=? AND direction IN ('BUY','SELL')
                    ORDER BY parsed_at DESC LIMIT 1
                """, (_REF_GROUP_ID,)).fetchone()
                day_start = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()
                today_count = con.execute("""
                    SELECT COUNT(*) FROM vantage_tg_signals
                    WHERE group_id=? AND direction IN ('BUY','SELL')
                    AND parsed_at >= ?
                """, (_REF_GROUP_ID, day_start)).fetchone()[0]
                return (last_row["parsed_at"] if last_row else None), today_count
            finally:
                con.close()

        try:
            # Offloaded to the DB worker thread -- see _check_correlation.
            from backend.src.db import database as _mdb
            _result = await _mdb.to_db_thread(_fetch)
        except Exception as exc:
            _log.debug("[RE-Engine] cadence stats error: %s", exc)
            return 240.0, 0
        if _result is None:
            return 240.0, 0
        last_parsed_at, today_count = _result
        mins_since = (time.time() - last_parsed_at) / 60.0 if last_parsed_at else 240.0
        return mins_since, today_count

    def _classify_ref_level(self, price: float) -> str:
        """Heuristic: classify a REF signal's level type for pattern learning."""
        if price <= 0:
            return "unknown"
        nearest_10 = round(price / 10) * 10
        if abs(price - nearest_10) < 2:
            return "round_10"
        if abs(price - (nearest_10 + 5)) < 2:
            return "round_5"
        # Compare against the bot's own top scored/proximity-filtered
        # candidates from the last cycle first (cheap, and usually right --
        # these are the levels the bot itself was actually watching).
        cached_lvls = self._cached.get("levels", [])
        for lvl in cached_lvls:
            if abs(lvl.get("price", 0) - price) < 4:
                return lvl.get("type", "unknown")

        # Fall back to the FULL level set (asia/swing/round/congestion, no
        # proximity/score cutoff) rather than guessing "swing_high" -- the
        # cached top-8 candidates above are filtered to levels within
        # PROXIMITY_THRESHOLD_PTS of the bot's own price at cycle time, which
        # routinely excludes the level a REF signal actually fired at. That
        # previously meant almost every miss silently mislabeled the
        # ML pattern-learning data as "swing_high" regardless of what type
        # the level actually was.
        all_lvls = self._cached.get("all_levels", [])
        if all_lvls:
            nearest = min(all_lvls, key=lambda lvl: abs(lvl.get("price", 0) - price))
            if abs(nearest.get("price", 0) - price) < 10:
                return nearest.get("type", "unknown")

        return "unknown"
