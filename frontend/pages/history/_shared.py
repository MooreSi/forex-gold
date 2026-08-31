"""Helpers more than one history section needs.

The two broker-clock conversions are also imported by
tests/ui/test_history_session_attribution.py off the package root, so
__init__.py re-exports them.
"""
from datetime import date, datetime, timezone
from typing import Optional

from backend.src.controllers import history_controller as history_ctl
from backend.src.controllers import history_controller as _hist_ctl

import logging

_log = logging.getLogger(__name__)

_BROKER_OFFSET = 10800  # broker stores UTC+3 timestamps as-if-UTC

# Session key -> the label shown in the day-detail breakdown. Ordered
# chronologically through the trading day rather than by P&L: a fixed order
# makes two days comparable at a glance, which is the whole point of opening
# the same panel on different days.
_SESSION_LABELS = (
    ("asian",   "Asian"),
    ("london",  "London"),
    ("overlap", "Overlap (LDN+NY)"),
    ("ny",      "New York"),
)


def _broker_ts_to_uk_date(ts) -> Optional[date]:
    """UK local calendar date for a broker timestamp (UTC+3 stored as-if-UTC).

    A thin delegate: the conversion itself lives in the formatting service and
    is reached through history_controller, so the page holds no date logic of
    its own. Kept at page level under its original name because that is where
    tests/ui/test_history_session_attribution.py reaches for it, alongside
    _broker_ts_to_utc_hour below -- the two must never be derived from two
    different readings of the same clock.
    """
    return history_ctl.broker_ts_to_uk_date(ts)


def _broker_ts_to_utc_hour(ts) -> Optional[int]:
    """UTC hour-of-day for a broker timestamp, for session attribution.

    Uses the same _BROKER_OFFSET unwind as _broker_ts_to_uk_date so a trade's
    calendar day and its session can never be derived from two different
    readings of the same clock. UTC because that is what _session_for_hour
    expects -- the session boundaries are defined in UTC and the heatmap on
    this page already reads them that way.
    """
    try:
        return datetime.fromtimestamp(float(ts) - _BROKER_OFFSET, tz=timezone.utc).hour
    except Exception:
        return None


def _entry_deal_comments(by_pos: dict) -> dict:
    """{ticket_str: opening-deal comment} for every position that has one --
    the input _comment_attribution_maps works from. `by_pos` is
    {position_id: [deal, ...]} as both callers already build it."""
    out: dict[str, str] = {}
    for _pid, _ds in (by_pos or {}).items():
        for _d in _ds:
            if _d.get("entry") == 0 and (_d.get("comment") or ""):
                out[str(_pid)] = _d.get("comment")
                break
    return out


def _get_market_type_map(year: int, month: int) -> dict:
    """
    Returns {date: (label, colour)} for each day in the month using ADX values
    stored in test_analysis_log.adx (written by the signal generator every scan cycle).

    ADX thresholds (averaged across all scans on that day):
      ≥ 50  →  Strong Trend  (red)    — bad for bounce system
      ≥ 35  →  Trending      (orange) — risky for bounce
      ≥ 20  →  Mixed         (slate)  — transitional / moderate
      < 20  →  Ranging       (teal)   — ideal for bounce system

    A directional arrow (↑/↓) is appended when HTF bias is consistently
    bullish or bearish across the day's scans.
    """
    result: dict[date, tuple[str, str]] = {}
    try:
        if not _hist_ctl.signal_lab_is_available():
            return result

        if month == 12:
            next_month_first = date(year + 1, 1, 1)
        else:
            next_month_first = date(year, month + 1, 1)

        ts_start = datetime.combine(date(year, month, 1), datetime.min.time()).timestamp()
        ts_end   = datetime.combine(next_month_first,     datetime.min.time()).timestamp()

        rows = _hist_ctl.signal_lab_adx_and_bias_samples(ts_start, ts_end)

        # Group ADX + bias samples by date
        day_adx:  dict[date, list[float]] = {}
        day_bias: dict[date, list[str]]   = {}
        for r in rows:
            d = history_ctl.to_date(r["ts"])
            if not d:
                continue
            day_adx.setdefault(d, []).append(float(r["adx"]))
            if r["htf_bias"]:
                day_bias.setdefault(d, []).append(r["htf_bias"])

        for d, adx_list in day_adx.items():
            avg    = sum(adx_list) / len(adx_list)
            biases = day_bias.get(d, [])
            # Dominant HTF bias directional arrow
            if biases:
                bull = biases.count("bullish")
                bear = biases.count("bearish")
                if bull > bear * 1.5:
                    arrow = " ↑"
                elif bear > bull * 1.5:
                    arrow = " ↓"
                else:
                    arrow = ""
            else:
                arrow = ""

            if avg >= 50:
                result[d] = (f"Strong Trend{arrow}", "#ef4444")
            elif avg >= 35:
                result[d] = (f"Trending{arrow}",     "#f97316")
            elif avg >= 20:
                result[d] = (f"Mixed{arrow}",        "#94a3b8")
            else:
                result[d] = (f"Ranging{arrow}",      "#2dd4bf")

    except Exception as e:
        _log.debug("[history] market-type map lookup failed: %s", e)

    return result
