"""Copy linked broker evidence; never modify execution or the source databases."""
from __future__ import annotations
from collections import defaultdict
import json
import math
from . import observations as o
from . import features

TRADE_FIELDS = ("trade_id", "signal_id", "mt5_ticket", "direction", "entry_price", "initial_sl",
    "initial_risk", "lot_size", "remaining_lots", "status", "open_time", "close_time", "close_price",
    "net_pnl", "mt5_profit", "spread_cost", "commission", "swap_est", "slippage_cost", "strategy",
    "exit_reason", "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8")
PARTIAL_FIELDS = ("id", "trade_id", "ts", "lots_closed", "close_price", "pnl", "reason")


def capture(store, signals, trades, partials, snapshots, env, now, deals_by_ticket=None):
    by_ticket, by_trade, decisions = defaultdict(list), defaultdict(list), defaultdict(list)
    for trade in trades:
        by_ticket[(trade.get("signal_id"), trade.get("mt5_ticket"))].append(trade)
    for part in partials:
        by_trade[part["trade_id"]].append(part)
    for snapshot in snapshots:
        decisions[snapshot["signal_ref"]].append(snapshot)
    result = {"linked": 0, "ambiguous": 0, "labels": 0, "untrainable": 0}
    for signal in signals:
        linked = by_ticket[(signal.get("vantage_signal_id"), signal.get("mt5_ticket"))]
        if len(linked) != 1:
            result["ambiguous"] += 1
            continue
        trade = linked[0]
        opened = o.stamp(trade.get("open_time"))
        if opened is None:
            continue
        result["linked"] += 1
        store.put(env, {"source": "mt5_ledger", "kind": "broker_trade", "key": trade["trade_id"],
            "event_ts": opened, "available_at": now,
            "payload": {k: trade.get(k) for k in TRADE_FIELDS}})
        for part in by_trade[trade["trade_id"]]:
            if o.stamp(part.get("ts")) is None:
                continue
            store.put(env, {"source": "mt5_ledger", "kind": "broker_partial", "key": str(part["id"]),
                "event_ts": part["ts"], "available_at": now,
                "payload": {k: part.get(k) for k in PARTIAL_FIELDS}})
        label = o.broker_label(signal, trade, now)
        if label is None:
            result["untrainable"] += int(trade.get("status") == "closed")
            continue
        available = label.pop("available_at")
        eligible = [s for s in decisions[signal["signal_ref"]] if s["decision_ts"] <= opened]
        chosen = max(eligible, key=lambda s: (s["stage"] == "fill", s["decision_ts"]), default=None)
        base = None
        if chosen and chosen.get("features_json"):
            raw = json.loads(chosen["features_json"])
            if len(raw) == 38 and all(o.finite(x) is not None for x in raw):
                base = raw
        ts = chosen["decision_ts"] if chosen else signal["created_at"]
        cashflows = (deals_by_ticket or {}).get(trade["mt5_ticket"], [])
        full_net = o.deal_net(cashflows) if o.deals_complete(cashflows, trade.get("lot_size")) else None
        label.update(costs_complete=full_net is not None,
                     ledger_net=label["net"], send_time=None,
                     send_time_status="not exposed by order transport",
                     broker_timestamp_basis="raw deal server time retained separately")
        if full_net is not None:
            label.update(net=full_net, r=full_net / label["risk"])
        label.update(features=base, decision_ts=ts,
                     external_features=features.vector(store.context_events(env, ts), ts),
                     snapshot_id=chosen["id"] if chosen else None,
                     policy_hash=chosen["policy_hash"] if chosen else None,
                     model_id=chosen["model_id"] if chosen else None,
                     champion_prediction=chosen["predicted_r"] if chosen else None,
                     sampling="executed trades only; rejected opportunities have no broker label")
        result["labels"] += int(store.put(env, {"source": "mt5_ledger", "kind": "broker_label",
            "key": trade["trade_id"], "event_ts": ts, "available_at": available, "payload": label}))
    return result
