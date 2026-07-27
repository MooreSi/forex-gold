"""
Per-message latency tracing for the Telegram → signal → order pipeline.

Records monotonic timestamps at each pipeline stage, keyed by Telegram
message id, so the gap between any two stages can be measured precisely —
independent of Telegram's own second-resolution message timestamp, which is
too coarse to measure sub-second network transit.

Stages (see docstring in telegram_reader.py / engine.py for exact call sites):
  t1_arrived   — NewMessage event handler fires (closest to wire arrival)
  t2_queued    — put onto the internal asyncio.Queue
  t3_dequeued  — _event_processor picks it up (gap t2->t3 = asyncio scheduling delay)
  t4_buffered  — message buffered + "MSG slot=" logged
  t5_woken     — scanner wake event set
  t6_scanning  — signal scanner loop starts processing this message
  t7_decided   — a trade decision was recorded (New signal / Signal queued)
  t8_ordered   — MT5 order POST fired

Cheap by design: a dict write per stage. Bounded ring buffer, in-memory only
(resets on restart) — this is a diagnostic tool, not a permanent audit log.
"""
from __future__ import annotations

import time
from typing import Optional

_MAX_TRACES = 1000
_traces: dict[str, dict[str, float]] = {}


def mark(msg_id, stage: str) -> None:
    if not msg_id:
        return
    key = str(msg_id)
    entry = _traces.get(key)
    if entry is None:
        entry = _traces[key] = {}
        if len(_traces) > _MAX_TRACES:
            # dicts preserve insertion order — drop the oldest quarter
            for k in list(_traces.keys())[: _MAX_TRACES // 4]:
                _traces.pop(k, None)
    entry[stage] = time.monotonic()


def get(msg_id) -> Optional[dict[str, float]]:
    return _traces.get(str(msg_id))


def gap_ms(msg_id, stage_a: str, stage_b: str) -> Optional[float]:
    entry = _traces.get(str(msg_id))
    if not entry or stage_a not in entry or stage_b not in entry:
        return None
    return (entry[stage_b] - entry[stage_a]) * 1000.0


_STAGE_PAIRS = [
    ("t1_arrived",  "t3_dequeued", "queue_wait_ms"),      # asyncio scheduling delay
    ("t3_dequeued", "t4_buffered", "buffer_ms"),
    ("t4_buffered", "t6_scanning", "scanner_wake_ms"),
    ("t6_scanning", "t7_decided",  "decision_ms"),
    ("t7_decided",  "t8_ordered",  "order_ms"),
    ("t1_arrived",  "t8_ordered",  "total_ms"),
]


def _percentiles(deltas: list[float]) -> dict:
    if not deltas:
        return {}
    s = sorted(deltas)
    n = len(s)

    def pct(p: float) -> float:
        return round(s[min(n - 1, int(n * p))], 1)

    return {"n": n, "p50": pct(0.5), "p90": pct(0.9), "p99": pct(0.99), "max": round(s[-1], 1)}


def summary() -> dict[str, dict]:
    """Percentile summary (ms) for each pipeline stage gap, across all traced
    messages that reached both endpoints of that gap."""
    out: dict[str, dict] = {}
    for a, b, label in _STAGE_PAIRS:
        deltas = [
            (e[b] - e[a]) * 1000.0
            for e in _traces.values()
            if a in e and b in e
        ]
        out[label] = _percentiles(deltas)
    return out


def clear() -> None:
    _traces.clear()
