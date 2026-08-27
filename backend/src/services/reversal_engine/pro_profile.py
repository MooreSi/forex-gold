"""What the reference channels' entries look like, as ML features (2026-08-05).

GOAL
----
Not "copy their signals" -- learn the market STRUCTURE present when a
professional decided a setup was worth taking, so the Reversal Engine can
weigh "does this moment resemble one they would act on" alongside its own
evidence.

HOW
---
pro_corpus.pro_snapshots (was the core db's tg_signal_snapshots until
2026-08-06) holds two kinds of row:
  stage != 'background'  a moment one of them actually fired  (positive)
  stage == 'background'  the market on a timer, no signal      (negative)

The positives alone can only describe. The contrast between the two is what
makes "why then" learnable, which is why background sampling exists.

Features are emitted as signed, normalised deltas rather than a single
verdict, so LightGBM can weigh each dimension itself instead of trusting a
score this module invented.

SUPERSEDED, NOT REPLACED (2026-08-06)
-------------------------------------
pro_model.py now learns the same question properly -- a classifier over the
same corpus, weighted by how each of their calls actually resolved. These
per-dimension deltas remain because the feature list is append-only and rows
were labelled with them, but pro_likeness is the feature meant to carry the
signal now.

WHY THIS REFUSES TO SPEAK TOO EARLY
-----------------------------------
Measured on the first 47 positives (2026-08-05), BOTH directions showed high
RSI -- BUYs at a median 68.3 and SELLs at 81.3. Read naively that says "they
buy AND sell when overbought", which is nonsense. The real cause is that
every sample came from one sustained rally (gold 4075 -> 4230 in two days),
so RSI was elevated the whole time regardless of what they did.

A profile built on that window encodes "gold rallied this week", not their
logic, and any model trained on it would learn a calendar artifact as though
it were a signal. So the gates below are deliberately strict: enough
positives, enough negatives, and evidence that the sample spans more than one
regime. Until all three hold, every feature returns its neutral value and the
model simply sees nothing -- which is correct, because we genuinely know
nothing yet.
"""
from __future__ import annotations

import json
import logging
import statistics
import time
from typing import Optional

log = logging.getLogger(__name__)

# Gates. Deliberately conservative -- see the module docstring.
_MIN_POSITIVES = 60
_MIN_NEGATIVES = 60
# The sample must contain genuinely different conditions. RSI spread is the
# cheapest honest proxy: a window where RSI never left 65-85 cannot tell us
# what they do in a range or a downtrend.
_MIN_RSI_SPREAD = 30.0

_NEUTRAL = {"pro_rsi_delta": 0.0, "pro_adx_delta": 0.0,
            "pro_fvg_delta": 0.0, "pro_profile_ready": 0.0}

_cache: dict = {"ts": 0.0, "profile": None}
_CACHE_TTL = 600.0


def _rows(background: bool):
    """Corpus rows from the Reversal Engine's shared database. Moved off the
    per-environment core db on 2026-08-06 -- see pro_corpus.py's docstring."""
    from backend.src.services.reversal_engine import pro_corpus_repo as pro_corpus
    return [(r.get("direction"), r.get("indicators_json"), r.get("fvg_json"))
            for r in pro_corpus.rows(background=background)]


def _extract(rows) -> dict:
    """-> {direction: {metric: [values]}}"""
    out: dict = {}
    for direction, ind_json, fvg_json in rows:
        d = (direction or "").upper()
        if d not in ("BUY", "SELL"):
            continue
        try:
            m15 = (json.loads(ind_json or "{}") or {}).get("M15") or {}
            fvg = json.loads(fvg_json or "{}") or {}
        except Exception:
            continue
        if not m15:
            continue
        b = out.setdefault(d, {"rsi": [], "adx": [], "fvg": []})
        for key, src, name in (("rsi14", m15, "rsi"), ("adx14", m15, "adx"),
                               ("fvg_confluence", fvg, "fvg")):
            v = src.get(key)
            if isinstance(v, (int, float)):
                b[name].append(float(v))
    return out


def build_profile(force: bool = False) -> Optional[dict]:
    """Median pro-entry conditions per direction, or None when not yet
    trustworthy. Cached, since this reads the whole snapshot table."""
    now = time.time()
    if not force and _cache["profile"] is not None and now - _cache["ts"] < _CACHE_TTL:
        return _cache["profile"]

    try:
        pos = _extract(_rows(background=False))
        neg = _extract(_rows(background=True))
    except Exception as e:
        log.debug("[ProProfile] read failed: %s", e)
        return None

    n_pos = sum(len(v["rsi"]) for v in pos.values())
    n_neg = sum(len(v["rsi"]) for v in neg.values())
    all_rsi = [x for v in pos.values() for x in v["rsi"]] + \
              [x for v in neg.values() for x in v["rsi"]]
    spread = (max(all_rsi) - min(all_rsi)) if all_rsi else 0.0

    reasons = []
    if n_pos < _MIN_POSITIVES:
        reasons.append(f"only {n_pos}/{_MIN_POSITIVES} pro signals")
    if n_neg < _MIN_NEGATIVES:
        reasons.append(f"only {n_neg}/{_MIN_NEGATIVES} background samples")
    if spread < _MIN_RSI_SPREAD:
        reasons.append(f"RSI spread {spread:.0f} < {_MIN_RSI_SPREAD:.0f} "
                       f"(single-regime sample)")
    if reasons:
        log.debug("[ProProfile] not ready: %s", "; ".join(reasons))
        _cache.update(ts=now, profile=None)
        return None

    profile = {}
    for d, v in pos.items():
        if len(v["rsi"]) < 15:
            continue
        profile[d] = {
            "rsi": statistics.median(v["rsi"]),
            "adx": statistics.median(v["adx"]) if v["adx"] else 0.0,
            "fvg": statistics.median(v["fvg"]) if v["fvg"] else 0.0,
            # Spread used to normalise deltas, floored so a tight sample
            # cannot turn a small difference into a huge z-score.
            "rsi_sd": max(statistics.pstdev(v["rsi"]) if len(v["rsi"]) > 1 else 10.0, 5.0),
            "adx_sd": max(statistics.pstdev(v["adx"]) if len(v["adx"]) > 1 else 10.0, 5.0),
            "n": len(v["rsi"]),
        }
    _cache.update(ts=now, profile=profile or None)
    return profile or None


def profile_features(direction: str, rsi: Optional[float], adx: Optional[float],
                     fvg_confluence: Optional[float]) -> dict:
    """Signed, normalised distance of the CURRENT moment from typical pro
    entries in this direction.

    0.0 on every field means "no usable profile" -- indistinguishable from
    "exactly average", which is intentional: both mean the feature carries no
    information, and a model should treat them identically.
    """
    prof = build_profile()
    if not prof:
        return dict(_NEUTRAL)
    p = prof.get((direction or "").upper())
    if not p:
        return dict(_NEUTRAL)
    out = dict(_NEUTRAL)
    out["pro_profile_ready"] = 1.0
    if isinstance(rsi, (int, float)):
        out["pro_rsi_delta"] = round(max(-3.0, min(3.0, (rsi - p["rsi"]) / p["rsi_sd"])), 4)
    if isinstance(adx, (int, float)):
        out["pro_adx_delta"] = round(max(-3.0, min(3.0, (adx - p["adx"]) / p["adx_sd"])), 4)
    if isinstance(fvg_confluence, (int, float)):
        out["pro_fvg_delta"] = round(fvg_confluence - p["fvg"], 4)
    return out


def status() -> str:
    """Human-readable readiness, for the report tool."""
    try:
        pos = _extract(_rows(background=False))
        neg = _extract(_rows(background=True))
    except Exception as e:
        return f"unavailable: {e}"
    n_pos = sum(len(v["rsi"]) for v in pos.values())
    n_neg = sum(len(v["rsi"]) for v in neg.values())
    all_rsi = [x for v in pos.values() for x in v["rsi"]] + \
              [x for v in neg.values() for x in v["rsi"]]
    spread = (max(all_rsi) - min(all_rsi)) if all_rsi else 0.0
    ready = build_profile(force=True) is not None
    lines = [f"pro signals   {n_pos}/{_MIN_POSITIVES}",
             f"background    {n_neg}/{_MIN_NEGATIVES}",
             f"RSI spread    {spread:.1f}/{_MIN_RSI_SPREAD:.0f}",
             f"READY: {ready}"]
    if ready:
        for d, p in (build_profile() or {}).items():
            lines.append(f"  {d}: rsi {p['rsi']:.1f} adx {p['adx']:.1f} "
                         f"fvg {p['fvg']:.2f} (n={p['n']})")
    return "\n".join(lines)
