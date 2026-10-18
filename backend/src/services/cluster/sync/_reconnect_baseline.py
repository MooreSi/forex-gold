"""What the Mac does with the VPS's welcome snapshot: its own settings win.

Until 2026-10-14 `_connect_once` mirrored every part of the welcome snapshot
down over the Mac's own database, and the only protection was the `_pending_*`
queue. That queue holds a change that went through a `propose_*` call and has
not been confirmed; anything that reached the Mac's database another way, and
any change whose queue was lost, was overwritten by the VPS's older value, and
the two nodes then agreed on the wrong one. Seen 2026-10-13: the Mac had been
the active node, the VPS was reconnected, and the VPS kept its old settings.

The rule (owner, 2026-10-14): the local node controls the settings. On every
connect this compares what the Mac holds with what the VPS reported, queues
each difference as a pending proposal (held, persisted and re-sent until the
VPS confirms, like any edit made in the UI), and only then mirrors down, which
skips what is queued. The queue settles itself: when the VPS's confirmed
snapshot equals it, it is dropped, so a connect that finds nothing different
changes nothing.

Two things stay the VPS's, because only the VPS can know them:

  * the circuit breaker's runtime state (`circuit_breaker_active_until`,
    `circuit_breaker_consec_losses`). A trip happens on the node that trades;
    sending the Mac's idle copy back would reset a breaker holding trading off.
  * `daily_target_resumed_day`, the day the operator resumed past the daily
    goal. "Absent, not empty, means leave it" (apply_trading_schedule_snapshot);
    sending the Mac's blank would re-arm a goal the operator resumed past.

An EMPTY local snapshot (schedule, strategy params, expert params) is never
sent: it means this install has nothing configured, not that the VPS should
lose its own.

Nothing here raises: it runs inside the receive loop, and an exception drops
the link and meets the same exception on the next connect.
"""
from __future__ import annotations

import logging

from backend.src.db import database as db_module
from backend.src.services.cluster.sync.synced_settings import SYNCED_SETTINGS_KEYS

log = logging.getLogger(__name__)

# Written by the node that trades, so they flow VPS -> Mac only.
VPS_OWNED_SETTINGS = frozenset({
    "circuit_breaker_active_until", "circuit_breaker_consec_losses",
})
VPS_OWNED_SCHEDULE_FIELDS = frozenset({"daily_target_resumed_day"})


def _same(a, b) -> bool:
    """True/1 and 3/3.0 are the same stored value; None is not 0."""
    if a is None or b is None:
        return a is b
    if isinstance(a, (bool, int, float)) and isinstance(b, (bool, int, float)):
        return float(a) == float(b)
    return a == b


class ClientBaselineMixin:
    def _adopt_peer_state(self, welcome: dict) -> None:
        """Take the VPS's welcome snapshot, keeping this node's own settings."""
        self.remote_settings = welcome.get("settings", {})
        self.remote_channel_strategy = welcome.get("channel_strategy", {})
        self.remote_trading_schedule = welcome.get("trading_schedule", {})
        self.remote_strategy_params = welcome.get("strategy_params", {})
        try:
            self._queue_local_baseline(welcome)
        except Exception as e:
            log.warning("[SyncClient] could not compare local settings with the "
                        "VPS on connect: %s", e)
        self._mirror_settings_locally(self.remote_settings)
        self._mirror_channel_strategy_locally(self.remote_channel_strategy)
        self._mirror_trading_schedule_locally(self.remote_trading_schedule)
        self._mirror_strategy_params_locally(self.remote_strategy_params)
        self._on_expert_params_state(welcome.get("expert_params"), connecting=True)

    def _queue_local_baseline(self, welcome: dict) -> None:
        queued = {
            "settings": self._queue_settings(welcome.get("settings") or {}),
            "channel strategy": self._queue_channel_strategy(welcome.get("channel_strategy") or {}),
            "trading schedule": self._queue_trading_schedule(welcome.get("trading_schedule") or {}),
            "strategy params": self._queue_strategy_params(welcome.get("strategy_params") or {}),
            "expert params": self._queue_expert_params(welcome.get("expert_params") or {}),
        }
        for what, keys in queued.items():
            if keys:
                log.info("[SyncClient] the VPS held %s differently from this node; "
                         "this node's values are being sent: %s", what, sorted(keys))

    def _guarded(self, read, what: str):
        try:
            return read()
        except Exception as e:
            log.warning("[SyncClient] could not read local %s to compare with the VPS: %s", what, e)
            return None

    def _queue_settings(self, remote: dict) -> list:
        local = self._guarded(db_module.get_risk_settings, "settings") or {}
        differs = {
            k: local[k] for k in SYNCED_SETTINGS_KEYS
            if k in local and k not in VPS_OWNED_SETTINGS
            and k not in self._pending_settings
            and not _same(local[k], remote.get(k))
        }
        if differs:
            self._pending_settings.update(differs)
            self._persist_pending()
        return list(differs)

    def _queue_channel_strategy(self, remote: dict) -> list:
        local = self._guarded(db_module.get_all_channel_strategy_overrides, "channel strategies") or {}
        differs = {
            src: entry for src, entry in local.items()
            if src not in self._pending_channel_strategy and entry != remote.get(src)
        }
        if differs:
            self._pending_channel_strategy.update(differs)
            self._persist_pending_channel_strategy()
        return list(differs)

    def _queue_trading_schedule(self, remote: dict) -> list:
        from backend.src.services.risk.schedule import trading_schedule_snapshot
        local = self._guarded(trading_schedule_snapshot, "trading schedule")
        if not local or self._pending_trading_schedule:
            return []
        local = {k: v for k, v in local.items() if k not in VPS_OWNED_SCHEDULE_FIELDS}
        differing = [k for k, v in local.items() if not _same_deep(v, remote.get(k))]
        if differing:
            self._pending_trading_schedule = local
            self._persist_pending_trading_schedule()
        return differing

    def _queue_strategy_params(self, remote: dict) -> list:
        from backend.src.services.risk.strategy_params import strategy_params_snapshot
        local = self._guarded(strategy_params_snapshot, "strategy params")
        if not local or self._pending_strategy_params:
            return []
        differing = [k for k, v in local.items() if not _same_deep(v, remote.get(k))]
        if differing:
            self._pending_strategy_params = local
            self._persist_pending_strategy_params()
        return differing

    def _queue_expert_params(self, remote: dict) -> list:
        from backend.src.services.risk.expert_params import snapshot
        local = self._guarded(snapshot, "expert params")
        if not local or self._pending_expert_params:
            return []
        differing = [k for k, v in local.items() if not _same_deep(v, remote.get(k))]
        if differing:
            self._pending_expert_params = local
            self._persist_pending_expert_params()
        return differing


def _same_deep(a, b) -> bool:
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same_deep(a[k], b[k]) for k in a)
    return _same(a, b)
