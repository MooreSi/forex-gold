"""
Sync client — runs on the Mac, connects out to the VPS's fixed IP.

Handles reconnect with backoff, mirrors the VPS's authoritative settings
into the local DB, surfaces VPS status for the Remote-mode dashboard, and
drives the STAND_DOWN/RESUME handshake behind the Local/Remote toggle.

Nothing here ever runs on its own — it is only active once the user has
entered a VPS host/port/token in Settings > Remote Node.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Callable, Optional

from backend.src.db import database as db_module
from backend.src.controllers.sync import tls_util
from backend.src.controllers.sync.protocol import (
    MSG_HELLO, MSG_WELCOME, MSG_REJECT, MSG_PING, MSG_PONG,
    MSG_STATUS_HEARTBEAT, MSG_SIGNAL_GEN_STATS, MSG_SETTINGS_PROPOSE, MSG_SETTINGS_STATE,
    MSG_SETTINGS_REJECTED, MSG_CHANNEL_STRATEGY_PROPOSE, MSG_CHANNEL_STRATEGY_STATE,
    MSG_STAND_DOWN, MSG_STAND_DOWN_ACK, MSG_RESUME,
    MSG_RESUME_ACK, MSG_TRADE_CLOSED, MSG_LEDGER_PULL, MSG_LEDGER_PUSH,
    MSG_MODEL_SNAPSHOT_REQUEST, MSG_MODEL_SNAPSHOT_UPLOAD,
    MSG_MODEL_SNAPSHOT_BEGIN, MSG_MODEL_SNAPSHOT_END,
    MSG_ENGINE_CONTROL, MSG_ENGINE_CONTROL_ACK,
    MSG_MARKET_ORDER, MSG_MARKET_ORDER_ACK, MSG_SIGNAL_ORDER, MSG_SIGNAL_ORDER_ACK,
    MSG_SIGNAL_FOLLOWUP, MSG_SIGNAL_FOLLOWUP_ACK,
    MSG_LEARNED_RULE_SYNC, MSG_AI_CONFIG_SYNC,
    MSG_AI_RECOVERED_SIGNAL_SYNC, MSG_AI_RECOVERED_PULL, MSG_AI_RECOVERED_PUSH,
    MSG_TRADING_SCHEDULE_PROPOSE, MSG_TRADING_SCHEDULE_STATE,
    MSG_STRATEGY_PARAMS_PROPOSE, MSG_STRATEGY_PARAMS_STATE,
    CONN_DISCONNECTED, CONN_CONNECTING, CONN_CONNECTED, CONN_REJECTED,
    TRADER_LOCAL, TRADER_REMOTE_VPS, make,
)

log = logging.getLogger("sync")

_RECONNECT_BACKOFF_S = (2, 5, 10, 20, 30)  # caps at 30s
_LEDGER_PULL_INTERVAL_S = 120
# Comfortably under the VPS's _LIVENESS_ALERT_THRESHOLD_S (90s, sync/server.py)
# with room for a missed beat or two under this app's own event-loop stalls.
# Without this, the VPS's "last message from the Mac" clock (_last_seen_ts)
# only advances on actual sync traffic (settings changes, forwarded trades,
# the 120s ledger/AI-recovered pulls above) — during any quiet stretch longer
# than 90s on an otherwise perfectly healthy connection, the liveness
# watchdog fired "Mac unreachable" even though the Mac was fully connected
# and the WebSocket transport-level ping/pong (ping_interval=20 below) was
# succeeding the whole time. MSG_PING/MSG_PONG already existed in the
# protocol and the server already replies to MSG_PING (sync/server.py
# _dispatch) — nothing was ever sending it from this side.
_LIVENESS_PING_INTERVAL_S = 20


class SyncClient:
    def __init__(self):
        self.conn_state: str = CONN_DISCONNECTED
        self.last_error: str = ""
        self.remote_status: dict = {}       # latest MSG_STATUS_HEARTBEAT payload
        self.remote_signal_gen_stats: dict = {}  # latest MSG_SIGNAL_GEN_STATS payload
        self.remote_settings: dict = {}     # latest confirmed settings snapshot
        # Mac-authored settings changes not yet confirmed applied on the VPS.
        # propose_settings() used to be fire-and-forget: if the connection
        # wasn't CONNECTED at that exact instant, or dropped before the VPS
        # replied, the change was silently lost — with reconnects happening
        # every 15-90s on this link, that was a real, recurring failure mode,
        # not a rare edge case. Anything here gets re-sent on every
        # reconnect and cleared only once the VPS's confirmed snapshot
        # actually reflects it.
        #
        # This dict is also mirrored to app_config (see _persist_pending/
        # _load_pending below) — living only in memory meant a Mac-side app
        # restart (or this object being recreated on a Local/Remote toggle)
        # before the VPS confirmed a change lost it permanently, with no
        # retry ever happening again. That's exactly the failure the user
        # hit: a risk-settings change that silently never reached the VPS.
        self._pending_settings: dict = self._load_pending()
        self.remote_channel_strategy: dict = {}  # latest confirmed channel-strategy snapshot
        # Same durability pattern as _pending_settings above, but keyed by
        # channel source rather than settings key.
        self._pending_channel_strategy: dict = self._load_pending_channel_strategy()
        self.remote_trading_schedule: dict = {}  # latest confirmed {enabled, schedule} snapshot
        # Same durability pattern as _pending_settings above, but a single
        # combined snapshot rather than a per-key dict — the whole 7-day/
        # 3-window config is edited and saved as one atomic unit in the UI.
        self._pending_trading_schedule: Optional[dict] = self._load_pending_trading_schedule()
        self.remote_strategy_params: dict = {}  # latest confirmed {strategy: {param: value}} snapshot
        # Same durability pattern as _pending_trading_schedule above -- a
        # single combined snapshot (all 5 strategies) rather than a per-key
        # dict, since _forward_strategy_params_over_sync always sends the
        # whole current snapshot.
        self._pending_strategy_params: Optional[dict] = self._load_pending_strategy_params()
        self._ws = None
        self._task: Optional[asyncio.Task] = None
        self._running = False
        self._host = self._port = self._token = ""
        self._on_stand_down_ack: Optional[Callable] = None
        self._on_resume_ack: Optional[Callable] = None
        self._stand_down_ack_event = asyncio.Event()
        self._resume_ack_event = asyncio.Event()
        self._last_stand_down_ack: dict = {}
        self._engine_control_ack_event = asyncio.Event()
        self._last_engine_control_ack: dict = {}
        self._market_order_ack_event = asyncio.Event()
        self._last_market_order_ack: dict = {}
        self._signal_order_ack_event = asyncio.Event()
        self._last_signal_order_ack: dict = {}
        self._signal_followup_ack_event = asyncio.Event()
        self._last_signal_followup_ack: dict = {}
        self._model_download_event = asyncio.Event()
        self._model_download_buf = bytearray()
        self._in_model_download = False
        self._pending_upload_bytes: Optional[bytes] = None

    # ── Pending-settings durability ──────────────────────────────────────────

    @staticmethod
    def _load_pending() -> dict:
        try:
            raw = db_module.get_app_config("sync_pending_settings")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _persist_pending(self) -> None:
        try:
            db_module.set_app_config("sync_pending_settings", json.dumps(self._pending_settings))
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending settings: %s", e)

    @staticmethod
    def _load_pending_channel_strategy() -> dict:
        try:
            raw = db_module.get_app_config("sync_pending_channel_strategy")
            return json.loads(raw) if raw else {}
        except Exception:
            return {}

    def _persist_pending_channel_strategy(self) -> None:
        try:
            db_module.set_app_config(
                "sync_pending_channel_strategy", json.dumps(self._pending_channel_strategy)
            )
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending channel strategy: %s", e)

    @staticmethod
    def _load_pending_trading_schedule() -> Optional[dict]:
        try:
            raw = db_module.get_app_config("sync_pending_trading_schedule")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _persist_pending_trading_schedule(self) -> None:
        try:
            db_module.set_app_config(
                "sync_pending_trading_schedule",
                json.dumps(self._pending_trading_schedule) if self._pending_trading_schedule else "",
            )
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending trading schedule: %s", e)

    @staticmethod
    def _load_pending_strategy_params() -> Optional[dict]:
        try:
            raw = db_module.get_app_config("sync_pending_strategy_params")
            return json.loads(raw) if raw else None
        except Exception:
            return None

    def _persist_pending_strategy_params(self) -> None:
        try:
            db_module.set_app_config(
                "sync_pending_strategy_params",
                json.dumps(self._pending_strategy_params) if self._pending_strategy_params else "",
            )
        except Exception as e:
            log.debug("[SyncClient] failed to persist pending strategy params: %s", e)

    # ── Config ───────────────────────────────────────────────────────────────

    def configure(self, host: str, port: int, token: str) -> None:
        """Persist the VPS connection details, including the token — encrypted
        at rest via core.secrets, the same Fernet-in-keychain pattern used for
        the MT5 password — so the Mac can auto-reconnect on app restart
        without the user re-pasting the token every session."""
        self._host, self._port, self._token = host, port, token
        from backend.src.config import secrets as _sec
        db_module.set_app_config("sync_remote_host", host)
        db_module.set_app_config("sync_remote_port", str(port))
        db_module.set_app_config("sync_remote_token_enc", _sec.encrypt(token))

    @staticmethod
    def load_config() -> tuple[str, int, str]:
        from backend.src.config import secrets as _sec
        host = db_module.get_app_config("sync_remote_host") or ""
        port = int(db_module.get_app_config("sync_remote_port") or tls_util.DEFAULT_SYNC_PORT)
        enc  = db_module.get_app_config("sync_remote_token_enc") or ""
        token = _sec.decrypt(enc) if enc else ""
        return host, port, token

    # ── Lifecycle ────────────────────────────────────────────────────────────

    def start(self, host: str, port: int, token: str) -> None:
        self._host, self._port, self._token = host, port, token
        self._running = True
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run_loop())

    def stop(self) -> None:
        self._running = False
        if self._task and not self._task.done():
            self._task.cancel()
        self.conn_state = CONN_DISCONNECTED

    async def _run_loop(self) -> None:
        attempt = 0
        while self._running:
            try:
                await self._connect_once()
                attempt = 0  # reset backoff after a clean connect
            except asyncio.CancelledError:
                break
            except Exception as e:
                self.last_error = str(e)
                log.info("[SyncClient] connection error: %s", e)
            self.conn_state = CONN_DISCONNECTED
            if not self._running:
                break
            delay = _RECONNECT_BACKOFF_S[min(attempt, len(_RECONNECT_BACKOFF_S) - 1)]
            attempt += 1
            await asyncio.sleep(delay)

    async def _connect_once(self) -> None:
        import websockets
        self.conn_state = CONN_CONNECTING
        uri = f"wss://{self._host}:{self._port}"
        ctx = tls_util.client_ssl_context()
        # ping_timeout was 20s — tight enough that this app's own occasional
        # multi-second event-loop stalls (seen up to ~9s under load) could
        # make an otherwise-healthy connection miss a pong and get killed,
        # producing the "no close frame received" / "timed out during
        # opening handshake" churn seen in the logs (reconnecting every
        # 15-90s for hours). 60s gives real headroom without materially
        # slowing detection of an actually-dead connection.
        async with websockets.connect(uri, ssl=ctx, ping_interval=20, ping_timeout=60) as ws:
            self._ws = ws
            await ws.send(json.dumps(make(MSG_HELLO, token=self._token)))
            hello_reply = json.loads(await asyncio.wait_for(ws.recv(), timeout=10.0))
            if hello_reply.get("type") == MSG_REJECT:
                self.conn_state = CONN_REJECTED
                self.last_error = hello_reply.get("reason", "rejected")
                log.warning("[SyncClient] VPS rejected connection: %s", self.last_error)
                return
            self.conn_state = CONN_CONNECTED
            self.remote_settings = hello_reply.get("settings", {})
            self._mirror_settings_locally(self.remote_settings)
            self.remote_channel_strategy = hello_reply.get("channel_strategy", {})
            self._mirror_channel_strategy_locally(self.remote_channel_strategy)
            self.remote_trading_schedule = hello_reply.get("trading_schedule", {})
            self._mirror_trading_schedule_locally(self.remote_trading_schedule)
            self.remote_strategy_params = hello_reply.get("strategy_params", {})
            self._mirror_strategy_params_locally(self.remote_strategy_params)
            log.info("[SyncClient] connected to VPS %s:%s", self._host, self._port)

            if self._pending_settings:
                log.info("[SyncClient] resending %d unconfirmed local settings "
                         "change(s) after reconnect: %s", len(self._pending_settings),
                         list(self._pending_settings.keys()))
                asyncio.create_task(self._flush_pending_settings())

            if self._pending_channel_strategy:
                log.info("[SyncClient] resending %d unconfirmed local channel "
                         "strategy change(s) after reconnect: %s",
                         len(self._pending_channel_strategy),
                         list(self._pending_channel_strategy.keys()))
                asyncio.create_task(self._flush_pending_channel_strategy())

            if self._pending_trading_schedule:
                log.info("[SyncClient] resending unconfirmed local trading "
                         "schedule change after reconnect")
                asyncio.create_task(self._flush_pending_trading_schedule())

            if self._pending_strategy_params:
                log.info("[SyncClient] resending unconfirmed local strategy "
                         "params change after reconnect")
                asyncio.create_task(self._flush_pending_strategy_params())

            asyncio.create_task(self._ledger_pull_loop())
            asyncio.create_task(self._ai_recovered_pull_loop())
            asyncio.create_task(self._liveness_ping_loop())

            async for raw in ws:
                if isinstance(raw, bytes):
                    if self._in_model_download:
                        self._model_download_buf.extend(raw)
                    continue
                msg = json.loads(raw)
                t = msg.get("type")
                if t == MSG_MODEL_SNAPSHOT_BEGIN:
                    self._model_download_buf = bytearray()
                    self._in_model_download  = True
                    continue
                if t == MSG_MODEL_SNAPSHOT_END:
                    self._in_model_download = False
                    from backend.src.controllers.sync.model_transfer import unpack_models, data_dir
                    try:
                        written = unpack_models(bytes(self._model_download_buf), data_dir())
                        log.info("[SyncClient] downloaded model snapshot from VPS: %s", written)
                    except Exception as e:
                        log.error("[SyncClient] failed to unpack downloaded model snapshot: %s", e)
                    self._model_download_buf = bytearray()
                    self._model_download_event.set()
                    continue
                await self._dispatch(msg)

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == MSG_STATUS_HEARTBEAT:
            self.remote_status = msg
        elif t == MSG_SIGNAL_GEN_STATS:
            self.remote_signal_gen_stats = msg
        elif t == MSG_SETTINGS_STATE:
            self.remote_settings = msg.get("settings", {})
            self._mirror_settings_locally(self.remote_settings)
            changed = False
            for k in list(self._pending_settings.keys()):
                if self.remote_settings.get(k) == self._pending_settings.get(k):
                    self._pending_settings.pop(k, None)
                    changed = True
            if changed:
                self._persist_pending()
        elif t == MSG_SETTINGS_REJECTED:
            log.warning("[SyncClient] settings change rejected by VPS: %s", msg.get("reason"))
        elif t == MSG_CHANNEL_STRATEGY_STATE:
            self.remote_channel_strategy = msg.get("channel_strategy", {})
            self._mirror_channel_strategy_locally(self.remote_channel_strategy)
            changed = False
            for source in list(self._pending_channel_strategy.keys()):
                confirmed = self.remote_channel_strategy.get(source) or {}
                pending = self._pending_channel_strategy.get(source) or {}
                if (confirmed.get("strategy") == pending.get("strategy")
                        and bool(confirmed.get("auto")) == bool(pending.get("auto"))):
                    self._pending_channel_strategy.pop(source, None)
                    changed = True
            if changed:
                self._persist_pending_channel_strategy()
        elif t == MSG_TRADING_SCHEDULE_STATE:
            self.remote_trading_schedule = msg.get("trading_schedule", {})
            self._mirror_trading_schedule_locally(self.remote_trading_schedule)
            if self._pending_trading_schedule == self.remote_trading_schedule:
                self._pending_trading_schedule = None
                self._persist_pending_trading_schedule()
        elif t == MSG_STRATEGY_PARAMS_STATE:
            self.remote_strategy_params = msg.get("strategy_params", {})
            self._mirror_strategy_params_locally(self.remote_strategy_params)
            if self._pending_strategy_params == self.remote_strategy_params:
                self._pending_strategy_params = None
                self._persist_pending_strategy_params()
        elif t == MSG_STAND_DOWN_ACK:
            self._last_stand_down_ack = msg
            self._stand_down_ack_event.set()
        elif t == MSG_RESUME_ACK:
            self._resume_ack_event.set()
        elif t == MSG_ENGINE_CONTROL_ACK:
            self._last_engine_control_ack = msg
            self._engine_control_ack_event.set()
        elif t == MSG_MARKET_ORDER_ACK:
            self._last_market_order_ack = msg
            self._market_order_ack_event.set()
        elif t == MSG_SIGNAL_ORDER_ACK:
            self._last_signal_order_ack = msg
            self._signal_order_ack_event.set()
        elif t == MSG_SIGNAL_FOLLOWUP_ACK:
            self._last_signal_followup_ack = msg
            self._signal_followup_ack_event.set()
        elif t == MSG_TRADE_CLOSED:
            node_id = msg.get("node_id", "")
            trade   = msg.get("trade", {})
            if node_id and trade.get("trade_id"):
                db_module.record_consolidated_trade(node_id, trade)
        elif t == MSG_LEARNED_RULE_SYNC:
            self._handle_learned_rule_sync(msg)
        elif t == MSG_LEDGER_PUSH:
            for row in msg.get("trades", []):
                db_module.record_consolidated_trade(row.get("node_id", ""), row)
        elif t == MSG_AI_RECOVERED_SIGNAL_SYNC:
            self._handle_ai_recovered_signal_sync(msg)
        elif t == MSG_AI_RECOVERED_PUSH:
            for row in msg.get("signals", []):
                self._apply_ai_recovered_snapshot_row(row)
        elif t == MSG_PONG:
            pass
        else:
            log.debug("[SyncClient] unhandled message type: %s", t)

    def _mirror_settings_locally(self, settings: dict) -> None:
        """Write the VPS's confirmed settings into this Mac's own settings
        table, so Local mode always starts from an identical baseline —
        see the handoff-consistency note in the design discussion.

        Skips any key with a pending outbound change: this fires on every
        reconnect (including right after the VPS restarts with its old,
        pre-change values), and without this guard it would overwrite a
        just-made local edit with the VPS's stale value before the pending
        resend below has a chance to land — a real, observed failure mode
        during the many VPS restarts today."""
        if not settings:
            return
        if self._pending_settings:
            settings = {k: v for k, v in settings.items() if k not in self._pending_settings}
            if not settings:
                return
        try:
            db_module.update_risk_settings(settings, _from_sync=True)
        except Exception as e:
            log.debug("[SyncClient] settings mirror failed: %s", e)

    def _mirror_channel_strategy_locally(self, snapshot: dict) -> None:
        """Mirror of _mirror_settings_locally() for per-channel strategy
        overrides — same pending-outbound skip to avoid a reconnect
        overwriting a just-made local edit with the VPS's stale value."""
        if not snapshot:
            return
        if self._pending_channel_strategy:
            snapshot = {k: v for k, v in snapshot.items() if k not in self._pending_channel_strategy}
            if not snapshot:
                return
        for source, entry in snapshot.items():
            try:
                db_module.set_channel_strategy_override(
                    source, entry.get("strategy"), bool(entry.get("auto")), _from_sync=True,
                )
            except Exception as e:
                log.debug("[SyncClient] channel strategy mirror failed for %s: %s", source, e)

    def _mirror_trading_schedule_locally(self, snapshot: dict) -> None:
        """Mirror of _mirror_settings_locally() for the Trading Schedule —
        same pending-outbound skip to avoid a reconnect overwriting a
        just-made local edit with the VPS's stale value."""
        if not snapshot:
            return
        if self._pending_trading_schedule:
            return
        try:
            from backend.src.services.risk.schedule import apply_trading_schedule_snapshot
            apply_trading_schedule_snapshot(snapshot)
        except Exception as e:
            log.debug("[SyncClient] trading schedule mirror failed: %s", e)

    def _mirror_strategy_params_locally(self, snapshot: dict) -> None:
        """Mirror of _mirror_settings_locally() for Strategy Parameters —
        same pending-outbound skip to avoid a reconnect overwriting a
        just-made local edit with the VPS's stale value."""
        if not snapshot:
            return
        if self._pending_strategy_params:
            return
        try:
            from backend.src.services.risk.strategy_params import apply_strategy_params_snapshot
            apply_strategy_params_snapshot(snapshot)
        except Exception as e:
            log.debug("[SyncClient] strategy params mirror failed: %s", e)

    def get_remote_open_position(self, mt5_ticket) -> Optional[dict]:
        """Look up a VPS-side open trade's full detail (strategy, TPs, SL,
        channel) by MT5 ticket from the last heartbeat, for rendering the
        Active Trade card on this node even though it has no local
        vantage_simulated_trades row for a trade the other node opened."""
        if not mt5_ticket:
            return None
        try:
            ticket_int = int(mt5_ticket)
        except (TypeError, ValueError):
            return None
        for pos in self.remote_status.get("open_positions", []):
            if pos.get("mt5_ticket") and int(pos["mt5_ticket"]) == ticket_int:
                return pos
        return None

    # ── Outbound actions ─────────────────────────────────────────────────────

    async def propose_settings(self, updates: dict) -> None:
        if updates:
            self._pending_settings.update(updates)
            self._persist_pending()
        await self._flush_pending_settings()

    async def _flush_pending_settings(self) -> None:
        """Send whatever's in _pending_settings if currently connected.
        Safe to call repeatedly and idempotent — resends the full pending
        set each time, so a message lost to a mid-flight disconnect is
        simply retried, and a stale VPS restart never loses track of what
        the Mac still wants applied."""
        if not self._pending_settings:
            return
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(
                    make(MSG_SETTINGS_PROPOSE, updates=dict(self._pending_settings))
                ))
            except Exception as e:
                log.debug("[SyncClient] settings propose send failed (will retry "
                          "on next reconnect): %s", e)

    async def propose_channel_strategy(self, source: str, strategy: Optional[str], auto: bool) -> None:
        self._pending_channel_strategy[source] = {"strategy": strategy, "auto": auto}
        self._persist_pending_channel_strategy()
        await self._flush_pending_channel_strategy()

    async def _flush_pending_channel_strategy(self) -> None:
        """Mirror of _flush_pending_settings() for channel strategy overrides."""
        if not self._pending_channel_strategy:
            return
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(
                    make(MSG_CHANNEL_STRATEGY_PROPOSE, updates=dict(self._pending_channel_strategy))
                ))
            except Exception as e:
                log.debug("[SyncClient] channel strategy propose send failed (will retry "
                          "on next reconnect): %s", e)

    async def propose_trading_schedule(self, snapshot: dict) -> None:
        self._pending_trading_schedule = snapshot
        self._persist_pending_trading_schedule()
        await self._flush_pending_trading_schedule()

    async def _flush_pending_trading_schedule(self) -> None:
        """Mirror of _flush_pending_settings() for the Trading Schedule."""
        if not self._pending_trading_schedule:
            return
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(
                    make(MSG_TRADING_SCHEDULE_PROPOSE, trading_schedule=self._pending_trading_schedule)
                ))
            except Exception as e:
                log.debug("[SyncClient] trading schedule propose send failed (will retry "
                          "on next reconnect): %s", e)

    async def propose_strategy_params(self, snapshot: dict) -> None:
        self._pending_strategy_params = snapshot
        self._persist_pending_strategy_params()
        await self._flush_pending_strategy_params()

    async def _flush_pending_strategy_params(self) -> None:
        """Mirror of _flush_pending_settings() for Strategy Parameters."""
        if not self._pending_strategy_params:
            return
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(
                    make(MSG_STRATEGY_PARAMS_PROPOSE, strategy_params=self._pending_strategy_params)
                ))
            except Exception as e:
                log.debug("[SyncClient] strategy params propose send failed (will retry "
                          "on next reconnect): %s", e)

    async def request_stand_down(self, timeout: float = 15.0) -> dict:
        """Send STAND_DOWN and wait for the VPS's ack (with its open-position
        summary). Raises TimeoutError if the VPS doesn't respond — the caller
        (the UI toggle handler) must NOT flip to Local mode without this ack,
        or the mutual-exclusion guarantee is void."""
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        self._stand_down_ack_event.clear()
        await self._ws.send(json.dumps(make(MSG_STAND_DOWN)))
        await asyncio.wait_for(self._stand_down_ack_event.wait(), timeout=timeout)
        return self._last_stand_down_ack

    async def request_resume(self, timeout: float = 15.0) -> None:
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        self._resume_ack_event.clear()
        await self._ws.send(json.dumps(make(MSG_RESUME)))
        await asyncio.wait_for(self._resume_ack_event.wait(), timeout=timeout)

    async def send_engine_control(self, engine: str, action: str,
                                  enabled: Optional[bool] = None, timeout: float = 10.0) -> dict:
        """Start/Stop/Run Now/set_ai_eval a sub-engine (breakout/bounce/reversal_engine)
        on the VPS, for use when this node is in Remote mode — the Signal
        Generator panels' buttons must act on the VPS's own engine, not this
        node's own local instance, which is stood down and would do nothing
        while the button looked like it worked. enabled is only used by
        set_ai_eval (the bounce/breakout AI-review toggle)."""
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        self._engine_control_ack_event.clear()
        kwargs = {"engine": engine, "action": action}
        if enabled is not None:
            kwargs["enabled"] = enabled
        await self._ws.send(json.dumps(make(MSG_ENGINE_CONTROL, **kwargs)))
        await asyncio.wait_for(self._engine_control_ack_event.wait(), timeout=timeout)
        return self._last_engine_control_ack

    async def send_market_order(self, direction: str, stop_loss: Optional[float] = None,
                                lot_size: Optional[float] = None, strategy: Optional[str] = None,
                                take_profit: Optional[float] = None, source_name: Optional[str] = None,
                                timeout: float = 15.0) -> dict:
        """Place a manual market order on the VPS's own account — for the
        Trading tab's Market Order button when this node is stood down
        (Remote mode). Without this the button just raised "Trading stood
        down" instead of actually doing what the user asked for. Mirrors
        send_engine_control()'s request/ack pattern; the VPS runs its own
        open_manual_market_order() and reports back the result or error.

        take_profit/source_name: added for the ORB/IVB Report tab's Execute
        Trade button — omitted (None) by the plain Market Order button,
        unchanged from before this was added."""
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        self._market_order_ack_event.clear()
        kwargs = {"direction": direction, "stop_loss": stop_loss,
                  "lot_size": lot_size, "strategy": strategy}
        if take_profit is not None:
            kwargs["take_profit"] = take_profit
        if source_name is not None:
            kwargs["source_name"] = source_name
        await self._ws.send(json.dumps(make(MSG_MARKET_ORDER, **kwargs)))
        await asyncio.wait_for(self._market_order_ack_event.wait(), timeout=timeout)
        return self._last_market_order_ack

    async def send_signal_order(self, signal_id: str, direction: str,
                                entry_low: float, entry_high: float, stop_loss: float,
                                tp1=None, tp2=None, tp3=None, tp4=None, tp5=None,
                                tp6=None, tp7=None, tp8=None,
                                lot_size: float = 0.01, strategy: Optional[str] = None,
                                tg_source: Optional[str] = None,
                                timeout: float = 15.0) -> dict:
        """Forward a fully-resolved trade from one of this node's own
        generators (Breakout/TestSignal/REopy/GD2-GD-VIP) to the VPS for
        execution — used only when centralized_signal_gen_enabled is on and
        the VPS is the active trader, so this node's generators keep
        analyzing while the VPS has stopped analyzing entirely (see
        core.database.should_generate_signals_here). Unlike
        send_market_order (a raw manual order), this carries open_trade()'s
        full parameter set: the VPS calls open_trade() directly with these
        exact values, not open_trade_from_signal, since risk sizing, session
        gates, and SL/TP logic have already been resolved here."""
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        self._signal_order_ack_event.clear()
        kwargs = {
            "signal_id": signal_id, "direction": direction,
            "entry_low": entry_low, "entry_high": entry_high, "stop_loss": stop_loss,
            "tp1": tp1, "tp2": tp2, "tp3": tp3, "tp4": tp4, "tp5": tp5,
            "tp6": tp6, "tp7": tp7, "tp8": tp8,
            "lot_size": lot_size, "strategy": strategy, "tg_source": tg_source,
        }
        await self._ws.send(json.dumps(make(MSG_SIGNAL_ORDER, **kwargs)))
        await asyncio.wait_for(self._signal_order_ack_event.wait(), timeout=timeout)
        return self._last_signal_order_ack

    async def send_signal_followup(self, channel_name: str, direction: str,
                                   updates: dict, tg_id: str,
                                   timeout: float = 15.0) -> dict:
        """Ask the VPS whether it has an open instant-entry trade from this
        channel/direction (opened via a forwarded MSG_SIGNAL_ORDER, so it only
        exists in the VPS's own vantage_simulated_trades) and, if so, apply
        this follow-up signal's SL/TP to it there. Returns {"matched": bool,
        "error"?: str} — the caller falls through to opening a new trade
        (which itself forwards normally) when matched is False."""
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        self._signal_followup_ack_event.clear()
        kwargs = {
            "channel_name": channel_name, "direction": direction,
            "updates": updates, "tg_id": tg_id,
        }
        await self._ws.send(json.dumps(make(MSG_SIGNAL_FOLLOWUP, **kwargs)))
        await asyncio.wait_for(self._signal_followup_ack_event.wait(), timeout=timeout)
        return self._last_signal_followup_ack

    async def push_trade_closed(self, trade: dict) -> None:
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            node_id = db_module.get_or_create_node_id()
            try:
                await self._ws.send(json.dumps(make(MSG_TRADE_CLOSED, node_id=node_id, trade=trade)))
            except Exception as e:
                log.debug("[SyncClient] push_trade_closed failed: %s", e)

    def _handle_learned_rule_sync(self, msg: dict) -> None:
        """A parser rule approved on the VPS — mirror it into this node's own
        channel_learned_rules so this Mac's independent Telethon session also
        parses future messages of this shape deterministically, instead of
        needing its own separate AI fallback + approval for the same format."""
        channel_name = msg.get("channel_name")
        pattern = msg.get("pattern")
        if not channel_name or not pattern:
            return
        db_module.save_synced_learned_rule(
            channel_name, msg.get("rule_type", "ai_derived_parser"), pattern,
            msg.get("action", "auto_parse"), msg.get("notes", ""),
            msg.get("source_msg_id"),
        )
        log.info("[SyncClient] mirrored learned rule from VPS: %s", channel_name)

    # ── AI-recovered signal review queue (Telegram > Reader Logic > AI tab) ──

    def _handle_ai_recovered_signal_sync(self, msg: dict) -> None:
        """Mirror a create/approve/rule_result/discard event from the VPS —
        see sync/protocol.py's MSG_AI_RECOVERED_SIGNAL_SYNC comment."""
        action = msg.get("action")
        tg_message_id = msg.get("tg_message_id")
        if not action or not tg_message_id:
            return
        if action == "created":
            if msg.get("message_type") == "sl_adjustment":
                db_module.save_ai_recovered_sl_adjustment(
                    tg_message_id, msg.get("channel_name", ""), msg.get("raw_text", ""),
                    msg.get("new_stop_loss"), msg.get("confidence", 0.0), msg.get("reasoning", ""),
                )
            else:
                parsed = {k: msg.get(k) for k in
                          ("direction", "entry_low", "entry_high", "stop_loss",
                           "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8")}
                db_module.save_ai_recovered_signal(
                    tg_message_id, msg.get("channel_name", ""), msg.get("raw_text", ""),
                    parsed, msg.get("confidence", 0.0), msg.get("reasoning", ""),
                )
        elif action == "approved":
            db_module.mark_ai_recovered_signal_approved_by_tg_id(tg_message_id)
        elif action == "rule_result":
            db_module.mark_ai_recovered_signal_rule_result_by_tg_id(
                tg_message_id, bool(msg.get("rule_generated")), msg.get("rule_gen_note", ""),
            )
        elif action == "discarded":
            db_module.discard_ai_recovered_signal_by_tg_id(tg_message_id)
        log.info("[SyncClient] mirrored ai_recovered_signal %s from VPS: %s", action, tg_message_id)

    def _apply_ai_recovered_snapshot_row(self, row: dict) -> None:
        """Applies one row of a MSG_AI_RECOVERED_PUSH full-queue snapshot —
        same upsert-by-tg_message_id path as a live 'created' event, just
        driven from a periodic full resync instead of a single push (see
        _ai_recovered_pull_loop). This is what backfills anything created
        while this node was disconnected, or before this sync feature
        existed at all."""
        tg_message_id = row.get("tg_message_id")
        if not tg_message_id:
            return
        if row.get("message_type") == "sl_adjustment":
            db_module.save_ai_recovered_sl_adjustment(
                tg_message_id, row.get("channel_name", ""), row.get("raw_text", ""),
                row.get("new_stop_loss"), row.get("confidence", 0.0), row.get("reasoning", ""),
            )
        else:
            parsed = {k: row.get(k) for k in
                      ("direction", "entry_low", "entry_high", "stop_loss",
                       "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8")}
            db_module.save_ai_recovered_signal(
                tg_message_id, row.get("channel_name", ""), row.get("raw_text", ""),
                parsed, row.get("confidence", 0.0), row.get("reasoning", ""),
            )

    async def push_ai_recovered_signal(self, action: str, **fields) -> None:
        """Best-effort, fire-and-forget — same pattern as push_learned_rule().
        A missed delivery is caught by the periodic full-queue pull instead
        (_ai_recovered_pull_loop)."""
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(make(
                    MSG_AI_RECOVERED_SIGNAL_SYNC, action=action, **fields,
                )))
            except Exception as e:
                log.debug("[SyncClient] push_ai_recovered_signal failed: %s", e)

    async def push_learned_rule(
        self, channel_name: str, rule_type: str, pattern: str, action: str,
        notes: str, source_msg_id: Optional[str],
    ) -> None:
        """Best-effort, fire-and-forget — same pattern as push_trade_closed().
        A rule approved on this Mac was already saved to this node's own DB
        before this is called; a missed delivery here just means the VPS
        keeps using its AI fallback for this message shape until it
        independently gets (and someone approves) the same extraction."""
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(make(
                    MSG_LEARNED_RULE_SYNC, channel_name=channel_name, rule_type=rule_type,
                    pattern=pattern, action=action, notes=notes, source_msg_id=source_msg_id,
                )))
            except Exception as e:
                log.debug("[SyncClient] push_learned_rule failed: %s", e)

    async def push_ai_config(self, updates: dict) -> None:
        """Mirror an AI provider/model/key change to the VPS (see
        SimulationEngine/Settings > AI's save handlers). Best-effort,
        fire-and-forget like push_trade_closed() — a missed delivery just
        means the VPS keeps its own AI config until the next successful
        save-on-Mac while connected."""
        if self._ws is not None and self.conn_state == CONN_CONNECTED:
            try:
                await self._ws.send(json.dumps(make(MSG_AI_CONFIG_SYNC, updates=updates)))
            except Exception as e:
                log.debug("[SyncClient] push_ai_config failed: %s", e)

    async def request_model_snapshot(self, direction: str, timeout: float = 60.0) -> None:
        """direction: 'download' (pull VPS models to this Mac) or 'upload'
        (push this Mac's models to the VPS). Runs on the same websocket the
        main receive loop is already reading, so download completion is
        signalled via an event set from within that loop rather than a
        second concurrent reader on the same connection."""
        if self._ws is None or self.conn_state != CONN_CONNECTED:
            raise ConnectionError("not connected to VPS")
        if direction == "download":
            self._model_download_event.clear()
            await self._ws.send(json.dumps(make(MSG_MODEL_SNAPSHOT_REQUEST)))
            await asyncio.wait_for(self._model_download_event.wait(), timeout=timeout)
        elif direction == "upload":
            from backend.src.controllers.sync.model_transfer import package_models, data_dir
            zip_bytes, names = package_models(data_dir())
            chunk_sz = 32 * 1024
            await self._ws.send(json.dumps(make(MSG_MODEL_SNAPSHOT_UPLOAD, files=names, size=len(zip_bytes))))
            for offset in range(0, len(zip_bytes), chunk_sz):
                await self._ws.send(zip_bytes[offset: offset + chunk_sz])
            await self._ws.send(json.dumps(make(MSG_MODEL_SNAPSHOT_END)))
            log.info("[SyncClient] uploaded model snapshot to VPS: %s", names)
        else:
            raise ValueError(f"unknown direction: {direction!r}")

    async def _liveness_ping_loop(self) -> None:
        """Keeps the VPS's _last_seen_ts fresh during quiet periods — see
        _LIVENESS_PING_INTERVAL_S above for why this is needed at all."""
        while self.conn_state == CONN_CONNECTED:
            try:
                if self._ws is not None:
                    await self._ws.send(json.dumps(make(MSG_PING)))
            except Exception:
                break
            await asyncio.sleep(_LIVENESS_PING_INTERVAL_S)

    async def _ledger_pull_loop(self) -> None:
        while self.conn_state == CONN_CONNECTED:
            try:
                if self._ws is not None:
                    await self._ws.send(json.dumps(make(MSG_LEDGER_PULL)))
            except Exception:
                break
            await asyncio.sleep(_LEDGER_PULL_INTERVAL_S)

    async def _ai_recovered_pull_loop(self) -> None:
        """Periodic full-queue resync, same cadence/shape as _ledger_pull_loop
        — catches anything a live push (push_ai_recovered_signal) missed
        while this node was briefly disconnected, and backfills any
        pre-existing backlog the first time this feature runs."""
        while self.conn_state == CONN_CONNECTED:
            try:
                if self._ws is not None:
                    await self._ws.send(json.dumps(make(MSG_AI_RECOVERED_PULL)))
            except Exception:
                break
            await asyncio.sleep(_LEDGER_PULL_INTERVAL_S)


_instance: Optional[SyncClient] = None


def get_instance() -> SyncClient:
    global _instance
    if _instance is None:
        _instance = SyncClient()
    return _instance
