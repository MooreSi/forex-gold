"""Optional licensed upgrades. No subscription is opened without explicit enablement."""
from __future__ import annotations
import asyncio
import json
import logging
import time
from urllib.parse import urlencode
from . import observations as o


async def calendar_stream(credential, emit):
    import websockets
    logger = logging.getLogger("re_evidence_private_ws")
    logger.propagate = False
    logger.setLevel(logging.CRITICAL)  # handshake URL carries the provider credential
    url = "wss://stream.tradingeconomics.com/?" + urlencode({"client": credential})
    async with websockets.connect(url, open_timeout=15, close_timeout=3, logger=logger) as ws:
        await ws.send(json.dumps({"topic": "subscribe", "to": "calendar"}))
        async for message in ws:
            row = json.loads(message)
            if isinstance(row, dict) and row.get("topic") == "calendar":
                event = o.calendar(row, time.time(), "tradingeconomics")
                if event:
                    await emit(event)


async def cme_stream(key, symbol, emit, client_factory=None):
    if client_factory is None:
        import databento
        client_factory = databento.Live
    client = client_factory(key=key)
    subscribed = False
    last = {}
    try:
        # subscribe may do blocking connection/auth I/O in some SDK releases.
        await asyncio.to_thread(client.subscribe, dataset="GLBX.MDP3", schema="mbp-1",
                                stype_in="raw_symbol", symbols=[symbol])
        subscribed = True
        async for record in client:
            if not hasattr(record, "levels"):
                continue
            seen = time.time()
            instrument = record.instrument_id
            if seen - last.get(instrument, 0) < 1:
                continue
            level = record.levels[0]
            row = {"hd": {"ts_event": record.ts_event, "instrument_id": instrument},
                   "levels": [{k: getattr(level, k) for k in ("bid_px", "ask_px", "bid_sz", "ask_sz")}],
                   "sequence": record.sequence}
            event = o.book(row, seen)
            if event:
                event["payload"].update(symbol=symbol, sampling="at most one MBP-1 observation/second/instrument")
                await emit(event)
                last[instrument] = seen
    finally:
        if subscribed:
            client.terminate()


async def historical_books(client, key, symbol, start, end, emit):
    # Explicit raw contract prevents silently joining different contracts at rollover.
    async with client.stream("POST", "https://hist.databento.com/v0/timeseries.get_range",
        auth=(key, ""), data={"dataset": "GLBX.MDP3", "schema": "mbp-1", "symbols": symbol,
            "stype_in": "raw_symbol", "start": start, "end": end, "encoding": "json",
            "pretty_px": "false", "pretty_ts": "false", "limit": "100000"}) as response:
        response.raise_for_status()
        async for line in response.aiter_lines():
            if not line.strip():
                continue
            event = o.book(json.loads(line), time.time())
            if event:
                event["payload"].update(symbol=symbol, historical_import=True,
                                         availability="first local import, never event time")
                await emit(event)
