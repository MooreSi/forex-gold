"""VIP-signal correlation tracking for GD Copy -- extracted verbatim (no
logic changes) from engine.py's _check_correlation/_classify_vip_level/
_vip_cadence_stats as part of task 040. See
docs/todo/refactor/backend-foundation/040-*.md.

_CorrelationMixin is composed into GDCopyEngine (gd_copy_signal_service.py).

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

from forex_trader.gd_copy_signal import gd_copy_signal_repo as gdc_db

_log = logging.getLogger("gd_copy_signal")

# Correlation semantics: our signal PREDICTS a VIP signal when the VIP entry
# arrives at the same level while our pending signal is still alive. We create
# zone signals as price APPROACHES a level; VIP posts when price ARRIVES -- so
# legitimate matches routinely lead by 10-30 minutes (near-miss data: 498 of
# 511 direction+price matches failed only on the old symmetric +/-300s window,
# with avg lead -836s and avg price distance 1.1pts). The window is therefore
# asymmetric: we may lead by up to the signal lifetime, or lag by 5 minutes
# (Telegram delivery latency).
_SIGNAL_MAX_AGE_S    = 7200    # 2-hour pending expiry (must match gd_copy_signal_service._SIGNAL_MAX_AGE_S)
_VIP_CORR_LEAD_MAX_S = _SIGNAL_MAX_AGE_S  # we fired first: up to 2h (signal lifetime)
_VIP_CORR_LAG_MAX_S  = 300     # VIP fired first: 5 min grace (delivery latency)
_VIP_CORR_PRICE_DELTA = 3.0    # pts between entry-zone midpoints (zones are 3-4pts wide)


class _CorrelationMixin:
    async def _check_correlation(self) -> None:
        """
        Scan recent real signals from BOTH covered channels (Gold Diggers
        VIP and Gold Diggers 2.0 / Institutional) in vantage_tg_signals and
        match them against our recent GDC signals.

        A match is:
          - Same direction
          - Entry zone midpoints within _VIP_CORR_PRICE_DELTA points
          - Time difference within the asymmetric lead/lag window
          - Same channel: a GDC signal only matches real signals from the
            channel it was modelled on (source_channel), never cross-channel.
        """
        _COVERED_CHANNELS = ("Gold Diggers VIP", "GOLD DIGGERS 2.0 ⚡️")
        # Recent real signals from either channel (last 4 hours) -- used
        # for the actual matching window.
        cutoff = time.time() - 14400

        def _fetch_vip_data():
            import sqlite3
            from forex_trader.config import get as cfg_get
            db_path = cfg_get("db_path", "")
            if not db_path:
                return None
            con = sqlite3.connect(db_path, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                _ph = ",".join("?" for _ in _COVERED_CHANNELS)
                vip_rows = con.execute(f"""
                    SELECT id, group_name, direction, entry_low, entry_high, parsed_at, status
                    FROM vantage_tg_signals
                    WHERE group_name IN ({_ph})
                    AND direction IN ('BUY','SELL')
                    AND parsed_at > ?
                    ORDER BY parsed_at DESC
                    LIMIT 100
                """, (*_COVERED_CHANNELS, cutoff)).fetchall()
                # True count of real signals received so far *today* (UTC), both
                # channels combined -- distinct from vip_rows above, which is only
                # a 4h rolling window used for matching.
                day_start = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()
                vip_today_count = con.execute(f"""
                    SELECT COUNT(*) FROM vantage_tg_signals
                    WHERE group_name IN ({_ph})
                    AND direction IN ('BUY','SELL')
                    AND parsed_at >= ?
                """, (*_COVERED_CHANNELS, day_start)).fetchone()[0]
                return vip_rows, vip_today_count
            finally:
                con.close()

        try:
            # Offloaded to the DB worker thread -- see test_signal.engine's
            # _reconcile_live_pnl for why a raw sqlite3.connect() here is a
            # whole-app hazard, not just a slow-task-local one.
            from forex_trader.core import database as _mdb
            _fetch_result = await _mdb.to_db_thread(_fetch_vip_data)
        except Exception as exc:
            _log.debug("[GDC-Engine] VIP fetch error: %s", exc)
            return
        if _fetch_result is None:
            return
        vip_rows, vip_today_count = _fetch_result

        # Build lookup of recent GDC signals (pending/triggered, last 4h)
        gdc_recent = [
            s for s in gdc_db.get_all_signals(limit=50)
            if float(s.get("created_at", 0)) > cutoff
        ]

        today  = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        predicted = 0
        lead_times: list[float] = []

        for gdc_sig in gdc_recent:
            if gdc_sig.get("correlation_confirmed"):
                continue  # already matched

            gdc_ts      = float(gdc_sig.get("created_at", 0))
            gdc_mid     = (float(gdc_sig.get("entry_low", 0) or 0)
                           + float(gdc_sig.get("entry_high", 0) or 0)) / 2
            gdc_dir     = gdc_sig.get("direction", "")
            gdc_channel = gdc_sig.get("source_channel") or "Gold Diggers VIP"

            for vip in vip_rows:
                # Never cross-correlate a GD2-modelled signal against a real
                # VIP message or vice versa -- each GDC signal is only ever
                # trying to predict the one channel it was built from.
                if vip["group_name"] != gdc_channel:
                    continue

                vip_mid = ((float(vip["entry_low"] or 0) + float(vip["entry_high"] or 0)) / 2)
                if vip_mid <= 0:
                    continue

                # Use parsed_at (already a float unix timestamp) -- more reliable
                # than parsing message_ts text which can have format variations.
                vip_ts = float(vip["parsed_at"] or 0)
                if vip_ts <= 0:
                    continue

                time_delta = gdc_ts - vip_ts   # negative = we were first
                dist = abs(gdc_mid - vip_mid)
                # Asymmetric window: our signal may LEAD the VIP post by its
                # whole pending lifetime (we predict the level before price
                # arrives) but only LAG it by the Telegram delivery grace.
                time_ok = -_VIP_CORR_LEAD_MAX_S <= time_delta <= _VIP_CORR_LAG_MAX_S
                matched = (vip["direction"] == gdc_dir
                           and dist <= _VIP_CORR_PRICE_DELTA
                           and time_ok)

                # Near-miss: direction matched but price/time missed the window --
                # log these (within widened tolerance) so thresholds can be tuned
                # empirically against real data instead of guessed constants.
                if (not matched and vip["direction"] == gdc_dir
                        and dist <= _VIP_CORR_PRICE_DELTA * 4
                        and -_VIP_CORR_LEAD_MAX_S * 2 <= time_delta <= _VIP_CORR_LAG_MAX_S * 4):
                    reason = []
                    if dist > _VIP_CORR_PRICE_DELTA:
                        reason.append("price")
                    if not time_ok:
                        reason.append("time")
                    gdc_db.log_near_miss(
                        gdc_signal_id=gdc_sig["id"],
                        vip_signal_id=str(vip["id"]),
                        direction=gdc_dir,
                        time_delta_s=round(time_delta, 1),
                        distance_pts=round(dist, 2),
                        reason="+".join(reason) or "unknown",
                    )

                if matched:

                    gdc_db.update_correlation(
                        sig_id=gdc_sig["id"],
                        vip_signal_id=str(vip["id"]),
                        time_delta_s=round(time_delta, 1),
                        distance_pts=round(dist, 2),
                    )
                    gdc_sig["correlation_confirmed"] = 1  # keep in-memory snapshot in sync for corr_total below

                    if time_delta < 0:  # we fired first -- that's the goal
                        predicted += 1

                    # Signed lead time: negative = we led, positive = we lagged
                    lead_times.append(time_delta)

                    # Feed VIP level type back to ML (pattern learning)
                    vip_level_type = self._classify_vip_level(vip_mid)
                    from forex_trader.gd_copy_signal import ml_engine as gdc_ml
                    gdc_ml.record_vip_signal(vip_level_type)

                    break  # one VIP match per GDC signal

        # avg_lead_time_s is signed: negative = we're ahead of GD VIP on average
        avg_lead = sum(lead_times) / len(lead_times) if lead_times else None

        # Query actual daily counts from DB rather than from the rolling 4h window.
        # The 4h window ages out confirmed signals within the same day, causing
        # gdc_correlated to be overwritten to 0 once correlations are >4h old.
        corr_total = gdc_db.count_today_correlated()
        today_sent = gdc_db.count_today_signals()
        corr_rate  = corr_total / today_sent if today_sent else 0.0

        gdc_db.upsert_daily_correlation(
            today,
            vip_signals_sent=vip_today_count,
            vip_predicted=predicted,
            gdc_correlated=corr_total,
            avg_lead_time_s=avg_lead,
            correlation_rate=round(corr_rate, 3),
        )

    async def _vip_cadence_stats(self) -> tuple[float, int]:
        """(minutes_since_last_real_vip_signal, vip_signals_received_today).
        Feeds the ML cadence features so the model can learn GD VIP's posting
        rhythm (e.g. quiet for 3h+ -> higher chance one is "due") instead of
        relying purely on static level geometry."""
        def _fetch():
            import sqlite3
            from forex_trader.config import get as cfg_get
            db_path = cfg_get("db_path", "")
            if not db_path:
                return None
            con = sqlite3.connect(db_path, timeout=5)
            con.row_factory = sqlite3.Row
            try:
                last_row = con.execute("""
                    SELECT parsed_at FROM vantage_tg_signals
                    WHERE group_name='Gold Diggers VIP' AND direction IN ('BUY','SELL')
                    ORDER BY parsed_at DESC LIMIT 1
                """).fetchone()
                day_start = datetime.now(timezone.utc).replace(
                    hour=0, minute=0, second=0, microsecond=0
                ).timestamp()
                today_count = con.execute("""
                    SELECT COUNT(*) FROM vantage_tg_signals
                    WHERE group_name='Gold Diggers VIP' AND direction IN ('BUY','SELL')
                    AND parsed_at >= ?
                """, (day_start,)).fetchone()[0]
                return (last_row["parsed_at"] if last_row else None), today_count
            finally:
                con.close()

        try:
            # Offloaded to the DB worker thread -- see _check_correlation.
            from forex_trader.core import database as _mdb
            _result = await _mdb.to_db_thread(_fetch)
        except Exception as exc:
            _log.debug("[GDC-Engine] cadence stats error: %s", exc)
            return 240.0, 0
        if _result is None:
            return 240.0, 0
        last_parsed_at, today_count = _result
        mins_since = (time.time() - last_parsed_at) / 60.0 if last_parsed_at else 240.0
        return mins_since, today_count

    def _classify_vip_level(self, price: float) -> str:
        """Heuristic: classify a VIP signal's level type for pattern learning."""
        if price <= 0:
            return "unknown"
        nearest_10 = round(price / 10) * 10
        if abs(price - nearest_10) < 2:
            return "round_10"
        if abs(price - (nearest_10 + 5)) < 2:
            return "round_5"
        # Compare against known Asia range
        cached_lvls = self._cached.get("levels", [])
        for lvl in cached_lvls:
            if abs(lvl.get("price", 0) - price) < 4:
                return lvl.get("type", "unknown")
        return "swing_high"  # most common for GD VIP BUY signals
