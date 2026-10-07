"""Licensed-feed protocol fakes, never external connections."""
import asyncio
from types import SimpleNamespace
from backend.src.services.reversal_engine.evidence.paid_feeds import cme_stream


def test_sdk_subscription_records_valid_book_and_terminates(monkeypatch):
    from backend.src.services.reversal_engine.evidence import paid_feeds
    calls, records = [], []
    class Client:
        def __init__(self, **kw): calls.append(kw)
        def subscribe(self, **kw): calls.append(kw)
        def __aiter__(self): return self
        async def __anext__(self):
            if records:
                raise StopAsyncIteration
            return SimpleNamespace(ts_event=99000000000, instrument_id=42, sequence=1,
                levels=[SimpleNamespace(bid_px=4095000000000, ask_px=4095100000000, bid_sz=10, ask_sz=10)])
        def terminate(self): calls.append("terminated")
    async def emit(event): records.append(event)
    monkeypatch.setattr(paid_feeds, "time", SimpleNamespace(time=lambda: 100), raising=False)
    asyncio.run(cme_stream("not-a-key", "GCZ6", emit, Client))
    assert calls == [{"key": "not-a-key"}, {"dataset": "GLBX.MDP3", "schema": "mbp-1",
                                         "stype_in": "raw_symbol", "symbols": ["GCZ6"]}, "terminated"]
    assert records[0]["payload"]["imbalance"] == 0
