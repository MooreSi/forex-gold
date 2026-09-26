"""
Per-signal latency tracing, read by Settings > Latency (docs/todo/006).

Records monotonic timestamps at each pipeline stage, keyed by a trace id, so
the gap between any two stages can be measured precisely -- independent of
Telegram's own second-resolution message timestamp, which is too coarse to
measure sub-second network transit.

Telegram pipeline, keyed by Telegram message id (untagged traces are this):
  t0_posted    — Telegram's own post time (wall clock, 1 s resolution)
  t1_arrived   — NewMessage event handler fires (closest to wire arrival)
  t2_queued    — put onto the internal asyncio.Queue
  t3_dequeued  — _event_processor picks it up (gap t2->t3 = asyncio scheduling delay)
  t4_buffered  — message buffered + "MSG slot=" logged
  t5_woken     — scanner wake event set
  t6_scanning  — signal scanner loop starts processing this message
  t7_decided   — a trade decision was recorded (New signal / Signal queued)
  t8_ordered   — the order call returned with a trade

Engine pipeline, keyed "<engine>:<signal id>" and tagged pipeline="engine":
  e1_created   — the engine stored the signal (wall clock)
  e2_exec_start — live execution began
  e3_ordered   — open_trade_from_signal returned with a trade

The FIRST stamp of a stage wins. The scanner re-reads the whole buffer every
pass, so a later stamp of t6 is a rescan of a message already handled, not
its pick-up.

Cheap by design: a dict write per stage. Bounded ring buffer, in-memory only
(resets on restart) — this is a diagnostic tool, not a permanent audit log.
"""
from __future__ import annotations

import time
from typing import Optional

_MAX_TRACES = 1000
_traces: dict[str, dict[str, float]] = {}
_meta: dict[str, dict] = {}

DEFAULT_PIPELINE = "telegram"


def _entry(key: str) -> dict[str, float]:
    entry = _traces.get(key)
    if entry is None:
        entry = _traces[key] = {}
        _meta[key] = {"at": time.time()}
        if len(_traces) > _MAX_TRACES:
            # dicts preserve insertion order — drop the oldest quarter
            for k in list(_traces.keys())[: _MAX_TRACES // 4]:
                _traces.pop(k, None)
                _meta.pop(k, None)
    return entry


def mark(msg_id, stage: str) -> None:
    if not msg_id:
        return
    _entry(str(msg_id)).setdefault(stage, time.monotonic())


def mark_at(msg_id, stage: str, wall_ts: float) -> None:
    """A stage whose time is known on the WALL clock (Telegram's post time,
    an engine's stored created_at), placed on the monotonic line."""
    if not msg_id or wall_ts is None:
        return
    mono = time.monotonic() - (time.time() - float(wall_ts))
    _entry(str(msg_id)).setdefault(stage, mono)


def tag(msg_id, pipeline: Optional[str] = None, label: Optional[str] = None) -> None:
    """Name a trace and say which pipeline it belongs to."""
    if not msg_id:
        return
    key = str(msg_id)
    _entry(key)
    if pipeline:
        _meta[key]["pipeline"] = pipeline
    if label:
        _meta[key]["label"] = label


def get(msg_id) -> Optional[dict[str, float]]:
    return _traces.get(str(msg_id))


def gap_ms(msg_id, stage_a: str, stage_b: str) -> Optional[float]:
    entry = _traces.get(str(msg_id))
    if not entry or stage_a not in entry or stage_b not in entry:
        return None
    return (entry[stage_b] - entry[stage_a]) * 1000.0


def entries(pipeline: Optional[str] = None) -> list[dict]:
    """Every trace, newest first: {key, pipeline, label, at, stages}."""
    out = []
    for key, stages in _traces.items():
        meta = _meta.get(key, {})
        pl = meta.get("pipeline", DEFAULT_PIPELINE)
        if pipeline is not None and pl != pipeline:
            continue
        out.append({"key": key, "pipeline": pl, "label": meta.get("label", ""),
                    "at": meta.get("at", 0.0), "stages": dict(stages)})
    out.sort(key=lambda e: e["at"], reverse=True)
    return out


def percentiles(deltas: list[float]) -> dict:
    if not deltas:
        return {}
    s = sorted(deltas)
    n = len(s)

    def pct(p: float) -> float:
        return round(s[min(n - 1, int(n * p))], 1)

    return {"n": n, "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "max": round(s[-1], 1)}


def clear() -> None:
    _traces.clear()
    _meta.clear()
