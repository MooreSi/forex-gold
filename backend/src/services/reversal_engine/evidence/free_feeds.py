"""Free proxies using existing dependencies and the shared cached calendar."""
from __future__ import annotations
import time
from . import observations as o


def futures_bars(rows, observed_at):
    out, previous = [], None
    for row in sorted(rows, key=lambda r: r["ts"]):
        ts, close, volume = o.finite(row.get("ts")), o.finite(row.get("close")), o.finite(row.get("volume"))
        if ts is None or close is None or volume is None or close <= 0 or volume < 0 or ts + 300 > observed_at:
            continue
        out.append({"source": "yahoo", "kind": "futures_bar", "key": "GC=F", "event_ts": ts + 300,
            "available_at": observed_at, "payload": {"close": close, "volume": volume,
                "return": (close - previous) / previous if previous is not None else 0.0,
                "return_missing": previous is None, "delayed": True, "interval_s": 300,
                "contract": "Yahoo GC=F front-contract proxy; roll mapping not supplied"}})
        previous = close
    return out


def fetch_futures():
    import yfinance as yf
    frame = yf.Ticker("GC=F").history(period="1d", interval="5m", timeout=15,
                                     auto_adjust=False, raise_errors=True)
    rows = [{"ts": index.timestamp(), "close": float(row["Close"]), "volume": float(row["Volume"])}
            for index, row in frame.iterrows()]
    return futures_bars(rows, time.time())


def fetch_calendar():
    from backend.src.utils import news_calendar
    # Shared public accessor preserves the existing cache, backoff and rate limits.
    rows = news_calendar.get_events(currencies={"USD", "XAU"}, impacts={"high", "medium", "low"})
    seen = time.time()
    out = []
    for row in rows:
        event = o.calendar({**row, "date": row["ts"]}, seen, "forexfactory")
        if event:
            out.append(event)
    return out
