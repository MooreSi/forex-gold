"""The lab's ONE replay simulator. Experiments configure it; they don't fork it.

Replays historical re_signals against the recovered ~60s price series:

    pending  : fill when a price sample enters [entry_low, entry_high]
               within max_age_s of creation (else 'expired')
    filled   : walk forward; at each sample check SL first (conservative
               tie-break, same as the backend backtest), then TPs in order
    ladder   : close `fractions[i]` of the position at TP i+1; SL moves to
               entry (break-even) after TP1 — matches observed engine
               behaviour (360/360 BE-after-TP1 in the snapshot)
    result   : P&L in R-multiples (risk units, full stop-out = -1 R)

Price-source caveat: 60s samples can't see intra-minute wicks. Both a TP and
the SL inside the same minute resolves as SL (pessimistic). Price that gaps
across the entry zone between samples is treated as no fill. Results are
DIRECTIONAL until re-run on real M1 candles (see lab README §data).

DATA GOTCHA (verified 2026-08-11): re_signals.stop_loss stores the FINAL
stop after break-even/trailing moves — 359 of 741 rows have it on the
profit side of the zone. The ORIGINAL stop is zone_mid -/+ sl_dist, and R
is normalised by sl_dist (that is what risk-based sizing used). run()
reconstructs this automatically; geometry overrides bypass it.

Hooks for experiments:
    signal_filter(row) -> bool          drop signals before they trade
    geometry(row) -> dict | None        override sl / tps / entry for a signal
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Optional

import numpy as np
import pandas as pd

DEFAULT_FRACTIONS = (0.8, 0.1, 0.05, 0.05)  # observed: remaining_frac 0.2 after TP1


@dataclass
class SimConfig:
    max_age_s: float = 7200.0            # engine's 2h pending expiry
    fractions: tuple = DEFAULT_FRACTIONS # fraction closed at TP1..TPn
    be_after_tp1: bool = True
    cost_R: float = 0.0                  # per-fill cost in R (spread+commission)
    horizon_s: float = 86400.0           # give up tracking after 24h in-trade
    signal_filter: Optional[Callable[[pd.Series], bool]] = None
    geometry: Optional[Callable[[pd.Series], Optional[dict]]] = None


@dataclass
class TradeResult:
    signal_id: int
    fate: str            # 'filtered' | 'expired' | 'win' | 'loss' | 'be' | 'open'
    r: float = np.nan    # realised R-multiple (NaN if never filled)
    max_tp_hit: int = 0
    fill_ts: float = np.nan
    close_ts: float = np.nan
    detail: dict = field(default_factory=dict)


def _ladder_prices(row: pd.Series, geo: Optional[dict]) -> tuple[float, float, float, list[float]]:
    """(entry_low, entry_high, original_sl, [tp1..]) honouring a geometry override.

    Reconstructs the ORIGINAL stop from zone_mid -/+ sl_dist because the
    stored stop_loss column is the final (post-BE/trail) stop — see module
    docstring. An explicit geometry {'stop_loss': ...} is used verbatim.
    """
    g = geo or {}
    lo = float(g.get("entry_low", row["entry_low"]))
    hi = float(g.get("entry_high", row["entry_high"]))
    mid = (lo + hi) / 2.0
    sign = 1.0 if str(row["direction"]).upper() == "BUY" else -1.0
    if "stop_loss" in g:
        sl = float(g["stop_loss"])
    else:
        sl_dist = float(row.get("sl_dist") or 0) or abs(mid - float(row["stop_loss"]))
        sl = mid - sign * sl_dist
    tps = g.get("tps")
    if tps is None:
        tps = [row.get(f"tp{i}") for i in range(1, 9)]
        tps = [float(t) for t in tps if t is not None and not pd.isna(t) and float(t) > 0]
    return lo, hi, sl, list(tps)


def run(signals: pd.DataFrame, prices: pd.DataFrame, cfg: SimConfig | None = None) -> pd.DataFrame:
    """Replay every signal row; return one TradeResult row per signal."""
    cfg = cfg or SimConfig()
    ts_arr = prices["ts"].to_numpy(dtype=float)
    px_arr = prices["price"].to_numpy(dtype=float)
    out: list[TradeResult] = []

    for _, row in signals.iterrows():
        sid = int(row["id"])
        if cfg.signal_filter is not None and not cfg.signal_filter(row):
            out.append(TradeResult(sid, "filtered"))
            continue

        geo = cfg.geometry(row) if cfg.geometry is not None else None
        lo, hi, sl, tps = _ladder_prices(row, geo)
        direction = str(row["direction"]).upper()
        created = float(row["created_at"])
        if not tps or sl <= 0:
            out.append(TradeResult(sid, "filtered", detail={"reason": "bad_geometry"}))
            continue

        i0 = int(np.searchsorted(ts_arr, created, side="left"))

        # --- fill phase -------------------------------------------------------
        fill_px = fill_ts = None
        i = i0
        while i < len(ts_arr) and ts_arr[i] - created <= cfg.max_age_s:
            if lo <= px_arr[i] <= hi:
                fill_px, fill_ts = px_arr[i], ts_arr[i]
                break
            i += 1
        if fill_px is None:
            out.append(TradeResult(sid, "expired"))
            continue

        entry = fill_px
        sign = 1.0 if direction == "BUY" else -1.0
        # R is normalised by the sized risk distance (sl_dist), not the
        # incidental fill-to-stop distance — matches risk-based sizing and
        # avoids near-zero-risk blowing up R.
        risk = abs(((lo + hi) / 2.0) - sl)
        if risk <= 0 or sign * (entry - sl) <= 0:
            out.append(TradeResult(sid, "filtered", detail={"reason": "bad_stop"}))
            continue
        tps = [t for t in tps if sign * (t - entry) > 0]  # keep TPs on the profit side
        if not tps:
            out.append(TradeResult(sid, "filtered", detail={"reason": "no_valid_tp"}))
            continue

        # --- management phase -------------------------------------------------
        remaining = 1.0
        realised_r = -cfg.cost_R
        cur_sl = sl
        tp_idx = 0
        fate, close_ts = "open", np.nan
        j = i + 1
        while j < len(ts_arr) and ts_arr[j] - fill_ts <= cfg.horizon_s:
            p = px_arr[j]
            # conservative tie-break: stop checked first
            if sign * (p - cur_sl) <= 0:
                realised_r += remaining * (sign * (cur_sl - entry)) / risk
                remaining = 0.0
                fate = "loss" if cur_sl == sl else ("be" if abs(realised_r) < 0.05 else "win")
                close_ts = ts_arr[j]
                break
            while tp_idx < len(tps) and sign * (p - tps[tp_idx]) >= 0:
                frac = cfg.fractions[tp_idx] if tp_idx < len(cfg.fractions) else 0.0
                frac = min(frac, remaining)
                realised_r += frac * (sign * (tps[tp_idx] - entry)) / risk
                remaining -= frac
                if tp_idx == 0 and cfg.be_after_tp1:
                    cur_sl = entry
                tp_idx += 1
                if tp_idx >= len(tps) or remaining <= 1e-9:
                    realised_r += remaining * (sign * (p - entry)) / risk
                    remaining = 0.0
                    fate = "win" if realised_r > 0 else ("be" if abs(realised_r) < 0.05 else "loss")
                    close_ts = ts_arr[j]
                    break
            if remaining <= 1e-9:
                break
            j += 1
        else:
            # ran out of data/horizon with position still (partly) open:
            # mark-to-last-sample so the R isn't silently dropped
            if remaining > 1e-9 and j - 1 < len(px_arr):
                realised_r += remaining * (sign * (px_arr[min(j, len(px_arr) - 1)] - entry)) / risk
            fate = "open"
            close_ts = ts_arr[min(j, len(ts_arr) - 1)]

        out.append(TradeResult(sid, fate, round(realised_r, 4), tp_idx, fill_ts, close_ts))

    res = pd.DataFrame([t.__dict__ for t in out])
    return res


def filled(res: pd.DataFrame) -> pd.DataFrame:
    """Rows that actually traded (closed or marked-open), for metrics."""
    return res[res["fate"].isin(["win", "loss", "be", "open"])]
