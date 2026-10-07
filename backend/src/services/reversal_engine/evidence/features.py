"""Arrival-aware features; delayed bars never masquerade as a live order book."""
from __future__ import annotations

FEATURE_NAMES = ["broker_spread", "broker_missing", "minutes_to_event", "calendar_missing",
                 "release_surprise", "release_missing", "cme_imbalance", "cme_spread", "cme_missing",
                 "futures_return", "futures_volume", "futures_age_minutes", "futures_missing"]


def context(events, decision_ts):
    latest = {}
    for event in sorted(events, key=lambda e: e["available_at"]):
        if event["available_at"] <= decision_ts:
            latest[(event["source"], event["kind"], event["key"])] = event
    batches = {e["source"]: set(e["payload"]["keys"]) for e in latest.values() if e["kind"] == "calendar_batch"}
    rows = [e for e in latest.values() if e["kind"] != "calendar" or e["source"] not in batches
            or e["key"] in batches[e["source"]]]
    out = {name: 0.0 for name in FEATURE_NAMES}
    out.update(broker_missing=1, calendar_missing=1, minutes_to_event=1440,
               release_missing=1, cme_missing=1, futures_missing=1, futures_age_minutes=1440)
    calendar = [r for r in rows if r["kind"] == "calendar" and r["payload"].get("currency") == "USD"]
    upcoming = [r for r in calendar if r["event_ts"] >= decision_ts
                and str(r["payload"].get("impact")).lower() in ("high", "3")]
    if upcoming:
        out.update(calendar_missing=0, minutes_to_event=min(1440, (min(r["event_ts"] for r in upcoming) - decision_ts) / 60))
    released = [r for r in calendar if 0 <= decision_ts - r["event_ts"] <= 3600
                and r["payload"].get("surprise") is not None and r["payload"].get("surprise_unit") == "%"]
    if released:
        release = max(released, key=lambda r: r["event_ts"])
        out.update(release_surprise=max(-10, min(10, release["payload"]["surprise"])), release_missing=0)
    for kind, age in (("broker_tick", 15), ("cme_book", 15), ("futures_bar", 3600)):
        usable = [r for r in rows if r["kind"] == kind and 0 <= decision_ts - r["event_ts"] <= age]
        if not usable:
            continue
        event = max(usable, key=lambda r: r["event_ts"])
        p = event["payload"]
        if kind == "broker_tick":
            out.update(broker_spread=p["spread"], broker_missing=0)
        elif kind == "cme_book":
            out.update(cme_imbalance=p["imbalance"], cme_spread=p["spread"], cme_missing=0)
        else:
            out.update(futures_return=p["return"], futures_volume=p["volume"],
                       futures_age_minutes=(decision_ts - event["event_ts"]) / 60, futures_missing=0)
    return out


def vector(events, decision_ts):
    measured = context(events, decision_ts)
    return [measured[k] for k in FEATURE_NAMES]
