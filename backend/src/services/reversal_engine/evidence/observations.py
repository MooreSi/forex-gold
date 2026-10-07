"""Pure normalisation. Arrival time is never reconstructed from an event time."""
from __future__ import annotations
import hashlib
import json
import math
import re
from datetime import datetime, timezone


def finite(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def stamp(value):
    if isinstance(value, (float, int)):
        return finite(value)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.replace(tzinfo=timezone.utc).timestamp() if parsed.tzinfo is None else parsed.timestamp()
    except (TypeError, ValueError):
        return None


def digest(payload):
    return hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False,
                                    separators=(",", ":")).encode()).hexdigest()


def broker_label(signal, trade, observed_at):
    risk, net, actual = (finite(trade.get(k)) for k in ("initial_risk", "net_pnl", "mt5_profit"))
    opened, closed, seen = (stamp(v) for v in
                            (trade.get("open_time"), trade.get("close_time"), observed_at))
    if (signal.get("live_exec_status") != "executed" or trade.get("status") != "closed"
        or not signal.get("mt5_ticket") or signal.get("mt5_ticket") != trade.get("mt5_ticket")
        or signal.get("vantage_signal_id") != trade.get("signal_id")
        or risk is None or risk <= 0 or net is None or actual is None
        or not math.isclose(net, actual, rel_tol=0, abs_tol=0.011)
        or opened is None or closed is None or seen is None or closed < opened or closed > seen
        or not math.isfinite(actual / risk)):
        return None
    return {"signal_ref": signal["signal_ref"], "trade_id": trade["trade_id"],
            "ticket": trade["mt5_ticket"], "strategy": trade.get("strategy") or signal.get("strategy"),
            "risk": risk, "net": actual, "r": actual / risk,
            "opened_at": opened, "closed_at": closed, "available_at": max(closed, seen)}


def _number(value):
    # Preserve units: only compare actual/consensus with identical suffixes.
    match = re.fullmatch(r"\s*([+-]?[\d,.]+)\s*([%KMBT]?)\s*", str(value))
    if not match:
        return None, None
    return finite(match[1].replace(",", "")), match[2]


def calendar(row, observed_at, source):
    event_ts, seen = stamp(row.get("date", row.get("Date"))), finite(observed_at)
    name = row.get("event", row.get("Event", row.get("title")))
    if event_ts is None or seen is None or not name:
        return None
    country = row.get("country", row.get("Country", ""))
    currency = row.get("currency", row.get("Currency"))
    if currency in (None, "", "null", "None"):
        currency = "USD" if country == "United States" else country
    actual, au = _number(row.get("actual", row.get("Actual")))
    forecast, fu = _number(row.get("forecast", row.get("Forecast")))
    # Scheduled future release cannot have an observed actual value.
    surprise = actual - forecast if event_ts <= seen and actual is not None and forecast is not None and au == fu else None
    payload = {"name": str(name), "currency": currency, "impact": row.get("impact", row.get("importance", row.get("Importance"))),
               "actual": row.get("actual", row.get("Actual")), "forecast": row.get("forecast", row.get("Forecast")),
               "previous": row.get("previous", row.get("Previous")), "surprise": surprise,
               "surprise_unit": au if surprise is not None else None, "scheduled_at": event_ts}
    key = str(row.get("calendarId", row.get("CalendarId")) or digest([name, currency, event_ts]))
    return {"source": source, "kind": "calendar", "key": key,
            "event_ts": event_ts, "available_at": seen, "payload": payload}


def book(row, observed_at):
    try:
        level = row["levels"][0]
        bid, ask = float(level["bid_px"]) / 1e9, float(level["ask_px"]) / 1e9
        bs, az = float(level["bid_sz"]), float(level["ask_sz"])
        event_ts = float(row["hd"]["ts_event"]) / 1e9
        instrument = str(row["hd"]["instrument_id"])
        if (not all(math.isfinite(v) for v in (bid, ask, bs, az, event_ts, observed_at))
            or bid <= 0 or ask < bid or min(bs, az) < 0 or bs + az <= 0
            or event_ts > observed_at + 1 or max(bid, ask) > 1e7):
            return None
        return {"source": "databento", "kind": "cme_book", "key": instrument,
                "event_ts": event_ts, "available_at": observed_at,
                "payload": {"bid": bid, "ask": ask, "spread": ask - bid,
                            "imbalance": (bs - az) / (bs + az), "instrument": instrument,
                            "schema": "mbp-1", "sequence": row.get("sequence")}}
    except (KeyError, IndexError, TypeError, ValueError):
        return None


def tick(tick, observed_at):
    bid, ask = finite(getattr(tick, "bid", None)), finite(getattr(tick, "ask", None))
    ts = stamp(getattr(tick, "timestamp", None))
    if bid is None or ask is None or ts is None or bid <= 0 or ask < bid or ts > observed_at + 1:
        return None
    return {"source": "mt5", "kind": "broker_tick", "key": "XAUUSD", "event_ts": ts,
            "available_at": observed_at, "payload": {"bid": bid, "ask": ask,
            "spread": ask - bid, "sampling": "5s observations, not every broker tick"}}


def deal_net(deals):
    """All deal cash flows once, including commissions charged on entry."""
    if not deals:
        return None
    total = 0.0
    for deal in deals:
        values = [finite(deal.get(k)) for k in ("profit", "swap", "fee", "commission")]
        if any(v is None for v in values):
            return None
        total += sum(values)
    return total if math.isfinite(total) else None


def deals_complete(deals, expected_lots=None):
    if not deals or any(d.get("entry") not in (0, 1, 3) or finite(d.get("volume")) is None for d in deals):
        return False
    opened = sum(float(d["volume"]) for d in deals if d["entry"] == 0)
    closed = sum(float(d["volume"]) for d in deals if d["entry"] in (1, 3))
    return (opened > 0 and math.isclose(opened, closed, rel_tol=0, abs_tol=1e-8)
            and (expected_lots is None or finite(expected_lots) is not None
                 and math.isclose(opened, float(expected_lots), rel_tol=0, abs_tol=1e-8)))
