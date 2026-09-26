"""Settings > Latency: where the time goes between a signal and an MT5 fill.

docs/todo/006. Two checkers, each a list of hops:

  telegram  Telegram post -> this app -> queue -> buffer -> scanner -> decide
            -> order (gates, sizing, EA/bridge, broker, ack)
  engine    a Breakout / Reversal Engine signal -> execution -> order

Each hop is measured from real signals (`utils/latency_trace`), plus a set of
live probes that time the infrastructure those hops cross right now. When this
node is paired with a VPS the same report is fetched from the VPS over the
sync link's ping/pong, with the round trip between the two.

**Nothing here places, modifies or closes an order.** The probes are a bridge
health read, a fresh tick, the EA's own ping, one light Telegram request, a
DB worker round trip and an event-loop timer. The order hop is measured only
from orders the app placed anyway.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from backend.src.services.diagnostics import broker_exec_log as _broker_log
from backend.src.utils import latency_trace as _lt

log = logging.getLogger(__name__)

# (id, label, what it covers, from stage, to stage, amber above this many ms).
# Thresholds are display-only: they colour a hop, they decide nothing.
TELEGRAM_HOPS = [
    ("delivery", "Telegram post → this app",
     "Telegram's post time to the message event firing here. Telegram stamps "
     "whole seconds and this machine's clock is not Telegram's, so read it to "
     "about +/-1 s.", "t0_posted", "t1_arrived", 2000),
    ("queue", "Event queue", "The event handler to the processor picking it up "
     "(asyncio scheduling).", "t1_arrived", "t3_dequeued", 100),
    ("buffer", "Buffer", "Recorded into the message buffer.",
     "t3_dequeued", "t4_buffered", 100),
    ("pickup", "Scanner pick-up", "Buffered to the signal scanner starting on it.",
     "t4_buffered", "t6_scanning", 500),
    ("decide", "Parse + decide", "Parsing, channel config, staleness and the "
     "strategy decision.", "t6_scanning", "t7_decided", 1000),
    ("order", "Order: gates, sizing, EA/bridge, broker, ack",
     "Pre-trade gates and sizing, the hand-off to the EA or the bridge, the "
     "broker's execution and the acknowledgement. With a VPS executing, this "
     "includes the trip to the VPS and back.", "t7_decided", "t8_ordered", 3000),
    ("total", "Total: arrival → order confirmed", "Everything after the message "
     "reached this machine.", "t1_arrived", "t8_ordered", 5000),
]

ENGINE_HOPS = [
    ("trigger", "Signal stored → execution start", "Breakout: the 5 s trigger "
     "check picking up a stored signal. Reversal Engine signals wait for price "
     "to reach their zone, so theirs is market time, not delay.",
     "e1_created", "e2_exec_start", 10000),
    ("order", "Execution: gates, sizing, EA/bridge, broker, ack",
     "The engine's fill-time checks (schedule, news, exposure, a momentum "
     "re-check on fresh candles), then open_trade_from_signal start to "
     "finish. With a VPS executing, this includes the trip to the VPS and "
     "back.", "e2_exec_start", "e3_ordered", 3000),
    ("total", "Signal stored → order confirmed", "",
     "e1_created", "e3_ordered", 15000),
]

# Orders this node executed on behalf of its paired Mac (it is the VPS).
FORWARDED_HOPS = [
    ("order", "Forwarded order: received → executed",
     "A trade the Mac decided, executed here: gates, EA/bridge, broker, ack.",
     "f1_received", "f2_ordered", 3000),
]

PIPELINES = {"telegram": TELEGRAM_HOPS, "engine": ENGINE_HOPS, "forwarded": FORWARDED_HOPS}

PROBE_AMBER_MS = {"loop": 100, "db": 200, "bridge": 250, "tick": 250,
                  "ea": 600, "telegram": 1000}
VPS_LINK_AMBER_MS = 300
BROKER_P90_AMBER_MS = 1000

RECENT_LIMIT = 25


def _gap(stages: dict, a: str, b: str) -> Optional[float]:
    if a not in stages or b not in stages:
        return None
    ms = (stages[b] - stages[a]) * 1000.0
    return round(max(ms, 0.0), 1)


def pipeline_view(pipeline: str) -> dict:
    """Per-hop percentiles over every trace, and the newest traces hop by hop."""
    hops = PIPELINES[pipeline]
    traces = _lt.entries(pipeline)
    out_hops = []
    for hop_id, label, detail, a, b, amber in hops:
        samples = [g for g in (_gap(t["stages"], a, b) for t in traces) if g is not None]
        stats = _lt.percentiles(samples)
        out_hops.append({"id": hop_id, "label": label, "detail": detail,
                         "amber_ms": amber, "stats": stats,
                         "slow": bool(stats) and stats["p90"] > amber})
    recent = [{"key": t["key"], "label": t["label"], "at": t["at"],
               "hops": {h[0]: _gap(t["stages"], h[3], h[4]) for h in hops}}
              for t in traces[:RECENT_LIMIT]]
    return {"hops": out_hops, "recent": recent}


def snapshot() -> dict:
    return {name: pipeline_view(name) for name in PIPELINES}


def structural_waits() -> list[dict]:
    """Delays built into the design: loops that poll rather than react.

    Read from the engines' own constants so this cannot drift from them.
    """
    from backend.src.services.breakout_signal import breakout_signal_service as _bo
    from backend.src.services.reversal_engine import reversal_engine_service as _re
    return [
        {"id": "breakout_cycle", "label": "Breakout analysis cycle",
         "seconds": _bo._CYCLE_INTERVAL,
         "detail": "Candles are analysed once per cycle, so a setup that forms "
                   "just after one waits up to a full cycle (half on average)."},
        {"id": "reversal_cycle", "label": "Reversal Engine analysis cycle",
         "seconds": _re._CYCLE_INTERVAL_S,
         "detail": "Levels and pending zones are rebuilt once per cycle."},
        {"id": "trigger_poll", "label": "Trigger check (both engines)",
         "seconds": _bo._OUTCOME_INTERVAL,
         "detail": "Stored signals are checked against price this often: up to "
                   "this long between price reaching a zone and execution."},
    ]


def _ea_instance():
    from backend.src.services.broker import ea_bridge
    return ea_bridge.get_instance()


def _paired_client():
    """This node's sync client if it is paired with a VPS, else None."""
    try:
        from backend.src.services.cluster.sync.client import SyncClient, get_instance
        host, _, _ = SyncClient.load_config()
    except Exception:
        return None
    return get_instance() if host else None


def _result(name: str, ok: bool, ms: Optional[float], detail: str = "", **extra) -> dict:
    return {"ok": ok, "ms": None if ms is None else round(ms, 1), "detail": detail,
            "amber_ms": PROBE_AMBER_MS[name], **extra}


async def _timed(name: str, coro_fn, judge) -> dict:
    t0 = time.monotonic()
    try:
        value = await coro_fn()
    except Exception as e:
        return _result(name, False, None, f"{type(e).__name__}: {e}")
    ms = (time.monotonic() - t0) * 1000.0
    ok, detail = judge(value)
    return _result(name, ok, ms, detail)


async def _loop_lag() -> dict:
    """How late a 50 ms timer fires: time the event loop spent busy elsewhere."""
    t0 = time.monotonic()
    await asyncio.sleep(0.05)
    return _result("loop", True, max(0.0, (time.monotonic() - t0) * 1000.0 - 50.0))


async def run_probes(engine: Any, reader: Any) -> dict:
    """Every live probe, each one a result and never an exception."""
    from backend.src.db import database as db_module
    out: dict = {"loop": await _loop_lag()}
    out["db"] = await _timed("db", lambda: db_module.to_db_thread(lambda: None),
                             lambda _v: (True, ""))
    if engine is None:
        out["bridge"] = _result("bridge", False, None, "no trading runtime")
        out["tick"] = _result("tick", False, None, "no trading runtime")
    else:
        out["bridge"] = await _timed(
            "bridge", engine.get_bridge_health,
            lambda h: (bool((h or {}).get("connected")),
                       "" if (h or {}).get("connected") else str((h or {}).get("error") or "bridge not connected")))
        out["tick"] = await _timed(
            "tick", engine.get_fresh_tick,
            lambda t: (t is not None, "" if t is not None else "no tick returned"))
    ea = _ea_instance()
    if ea is None:
        out["ea"] = _result("ea", False, None, "EA not connected")
    else:
        r = await ea.ping_ms()
        out["ea"] = _result("ea", r["ok"], r["ms"], r["detail"])
    rt = getattr(reader, "api_round_trip", None)
    if rt is None:
        out["telegram"] = _result("telegram", False, None, "no Telegram reader on this node")
    else:
        r = await rt()
        out["telegram"] = _result("telegram", r["ok"], r["ms"], r["detail"],
                                  session_dc=r.get("session_dc"), nearest_dc=r.get("nearest_dc"))
    return out


async def local_report(engine: Any, reader: Any) -> dict:
    """This node's probes, traces and broker execution times."""
    broker = await asyncio.to_thread(_broker_log.summary)
    for s in broker.get("servers", {}).values():
        s["slow"] = s["p90_ms"] > BROKER_P90_AMBER_MS
    return {"generated_at": time.time(), "probes": await run_probes(engine, reader),
            "pipelines": snapshot(), "structural": structural_waits(),
            "broker": broker, "broker_amber_ms": BROKER_P90_AMBER_MS}


async def vps_report() -> Optional[dict]:
    """The Mac<->VPS round trip and the VPS's own report; None when unpaired."""
    client = _paired_client()
    if client is None:
        return None
    r = await client.probe_peer()
    return {**r, "amber_ms": VPS_LINK_AMBER_MS}


def passive_view() -> dict:
    """What the tab shows before any check: traces, waits, and whether a VPS
    is paired (so it can say a check will include it)."""
    return {"pipelines": snapshot(), "structural": structural_waits(),
            "paired": _paired_client() is not None}


async def check(engine: Any, reader: Any) -> dict:
    """Everything, for the Run check button."""
    return {"local": await local_report(engine, reader), "vps": await vps_report()}
