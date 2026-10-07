"""Own background collectors. No order collaborators; blocking work stays off-loop."""
from __future__ import annotations
import asyncio
import importlib.util
import logging
import os
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import httpx
from backend.src import config
from .evidence_repo import EvidenceStore, broker_rows
from . import broker_capture, experiments, free_feeds, observations, paid_feeds, tracking

log = logging.getLogger(__name__)


def paths():
    env = str(config.get("account_env", "demo"))
    if env not in ("demo", "live"):
        raise ValueError("unknown account environment")
    data = Path(config.DATA_DIR)
    return env, data, Path(config.get("db_path", str(data / f"forex_trader_{env}.db")))


async def _repeat(store, name, interval, job):
    while True:
        try:
            detail = await job()
            await asyncio.to_thread(store.health, name, "ok", str(detail), time.time())
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await asyncio.to_thread(store.health, name, "unavailable", type(exc).__name__, time.time())
        await asyncio.sleep(interval)


class Worker:
    def __init__(self, engine, store):
        self.bridge, self.store = engine._bridge, store
        self.last_range = 0.0
        self.deal_cache = {}
        self.deal_attempt = {}
        self.final_deals = set()
        self.recovered = set()

    async def ticks(self):
        env, _, _ = paths()
        events, seen = [], time.time()
        if seen - self.last_range >= 30 and hasattr(self.bridge, "get_ticks_range"):
            start = max(seen - 60, self.last_range - 1) if self.last_range else seen - 30
            try:
                rows = await asyncio.wait_for(self.bridge.get_ticks_range(start, seen), timeout=15)
                arrived = time.time()
                for row in rows or []:
                    event = observations.tick(SimpleNamespace(bid=row.get("bid"), ask=row.get("ask"),
                                                               timestamp=row.get("time")), arrived)
                    if event:
                        event["payload"].update(sampling="bridge tick range", last=row.get("last"),
                                                volume=row.get("volume"), flags=row.get("flags"))
                        events.append(event)
                await asyncio.to_thread(self.store.health, "broker_tick_range", "ok" if rows else "empty",
                                        "range import; empty responses cannot prove complete history", arrived)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await asyncio.to_thread(self.store.health, "broker_tick_range", "unavailable", type(exc).__name__, time.time())
            self.last_range = seen
        # A separate sampled observation keeps freshness measurable between range imports.
        tick = await asyncio.wait_for(self.bridge.get_fresh_tick(), timeout=6)
        seen = time.time()
        event = observations.tick(tick, seen)
        if event:
            events.append(event)
        if paths()[0] != env:
            return "environment changed; observations discarded"
        if not events:
            raise ValueError("no valid broker ticks")
        count = await asyncio.to_thread(self.store.put_many, env, events)
        return f"{count} new quotes; range gaps after disconnect are not backfilled"

    async def broker(self):
        env, data, main = paths()
        rows = await asyncio.to_thread(broker_rows, main, data / "reversal_engine.db")
        signals, trades, partials, snapshots = rows
        if env not in self.recovered:
            saved = await asyncio.to_thread(self.store.by_kind, env, "broker_deals")
            for event in saved:
                self.deal_cache[(env, event["payload"]["ticket"])] = event["payload"]["deals"]
            self.recovered.add(env)
        now = time.time()
        # Closed trades with fully measured deal cashflows need no repeated broker request.
        tickets = {s.get("mt5_ticket") for s in signals}
        recent = sorted((t for t in trades if t.get("mt5_ticket") in tickets and float(t.get("close_time") or now) >= now - 86400),
                        key=lambda t: float(t.get("open_time") or 0), reverse=True)
        for trade in recent[:8]:
            ticket = trade["mt5_ticket"]
            cached = self.deal_cache.get((env, ticket))
            if cached and trade["status"] == "closed" and observations.deals_complete(cached, trade.get("lot_size")) and observations.deal_net(cached) is not None:
                continue
            if now - self.deal_attempt.get((env, ticket), 0) < 600:
                continue
            self.deal_attempt[(env, ticket)] = now
            if not hasattr(self.bridge, "get_position_history"):
                continue
            deals = await asyncio.wait_for(self.bridge.get_position_history(ticket), timeout=15)
            if paths()[0] != env:
                return "environment changed; broker deals discarded"
            if deals:
                clean = [{k: d.get(k) for k in ("ticket", "position_id", "entry", "type", "volume", "price",
                          "profit", "swap", "fee", "commission", "time", "time_msc")} for d in deals]
                self.deal_cache[(env, ticket)] = clean
                if trade["status"] == "closed" and observations.deals_complete(clean, trade.get("lot_size")) and observations.deal_net(clean) is not None:
                    self.final_deals.add((env, ticket))
                await asyncio.to_thread(self.store.put, env, {"source": "mt5", "kind": "broker_deals",
                    "key": str(ticket), "event_ts": float(trade.get("open_time") or now),
                    "available_at": time.time(), "payload": {"ticket": ticket, "deals": clean,
                        "clock": "broker server time; availability is local UTC arrival"}})
        if paths()[0] != env:
            return "environment changed; labels discarded"
        result = await asyncio.to_thread(broker_capture.capture, self.store, signals, trades, partials, snapshots,
                                        env, time.time(), {k[1]: v for k, v in self.deal_cache.items() if k[0] == env})
        return result

    async def free_calendar(self):
        events = await asyncio.to_thread(free_feeds.fetch_calendar)
        if not events:
            raise ValueError("calendar unavailable or empty")
        env, _, _ = paths()
        seen = max(e["available_at"] for e in events)
        events.append({"source": "forexfactory", "kind": "calendar_batch", "key": "current_week",
                       "event_ts": seen, "available_at": seen, "payload": {"keys": [e["key"] for e in events]}})
        await asyncio.to_thread(self.store.put_many, env, events)
        return "free calendar; actual release surprises unavailable"

    async def free_futures(self):
        events = await asyncio.to_thread(free_feeds.fetch_futures)
        if not events:
            raise ValueError("no closed futures bars")
        env, _, _ = paths()
        await asyncio.to_thread(self.store.put_many, env, events)
        return "delayed GC=F 5m bars; no exchange depth; last bar age=" + str(round((time.time() - events[-1]["event_ts"]) / 60, 1)) + "min"

    async def evaluate(self):
        env, data, _ = paths()
        result = await asyncio.to_thread(experiments.run_candidates, self.store, env, data / "reversal_experiments")
        await asyncio.to_thread(self.store.prune_ticks, time.time() - 7 * 86400)
        return f"{len(result)} new local experiment(s); models remain shadow candidates"

    async def emit(self, event):
        env, _, _ = paths()
        await asyncio.to_thread(self.store.put, env, event)
        provider = "te_calendar" if event["source"] == "tradingeconomics" else "cme"
        await asyncio.to_thread(self.store.health, provider, "ok", "stream observation received", time.time())

    async def calendar(self):
        await paid_feeds.calendar_stream(os.environ["RE_TE_CREDENTIAL"], self.emit)
        raise ConnectionError("calendar stream ended")

    async def cme(self):
        await paid_feeds.cme_stream(os.environ["DATABENTO_API_KEY"], os.environ["RE_CME_SYMBOL"], self.emit)
        raise ConnectionError("CME stream ended")

    async def mlflow(self):
        token = os.environ.get("MLFLOW_TRACKING_TOKEN")
        headers = {"Authorization": "Bearer " + token} if token else {}
        async with httpx.AsyncClient(timeout=15, headers=headers) as client:
            await tracking.export_pending(self.store, os.environ["MLFLOW_TRACKING_URI"], client)
        return "export pass complete; consult mlflow health for delivery failures"


async def run(engine, is_running):
    # Startup failure is contained by the owning context; no trading task depends on it.
    _, data, _ = paths()
    store = await asyncio.to_thread(EvidenceStore, data / "reversal_evidence.db")
    worker = Worker(engine, store)
    jobs = [("broker_ticks", 5, worker.ticks), ("broker_ledger", 60, worker.broker),
            ("free_calendar", 1800, worker.free_calendar), ("free_futures", 300, worker.free_futures),
            ("local_experiments", 900, worker.evaluate)]
    options = [("te_calendar", bool(os.environ.get("RE_TE_ENABLED") == "1" and os.environ.get("RE_TE_CREDENTIAL")), worker.calendar),
               ("cme", bool(os.environ.get("RE_CME_ENABLED") == "1" and os.environ.get("DATABENTO_API_KEY")
                            and os.environ.get("RE_CME_SYMBOL") and importlib.util.find_spec("databento")), worker.cme),
               ("mlflow_export", bool(os.environ.get("MLFLOW_TRACKING_URI")), worker.mlflow)]
    for name, enabled, job in options:
        if enabled:
            jobs.append((name, 60, job))
        else:
            await asyncio.to_thread(store.health, name, "unconfigured", "optional credentials/enablement/server/SDK absent; free collection and local tracking work", time.time())
    tasks = [asyncio.create_task(_repeat(store, name, interval, job), name="ReversalEvidence." + name)
             for name, interval, job in jobs]
    try:
        await asyncio.gather(*tasks)
    finally:
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


@asynccontextmanager
async def collector(engine, is_running, runner=None):
    task = None
    if getattr(engine, "_bridge", None) is not None:
        task = asyncio.create_task((runner or run)(engine, is_running), name="ReversalEvidence")
    try:
        yield
    finally:
        if task:
            task.cancel()
            result = await asyncio.gather(task, return_exceptions=True)
            if isinstance(result[0], Exception):
                log.warning("[RE-Evidence] collector stopped: %s", type(result[0]).__name__)
