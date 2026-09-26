"""
Sync server — runs on the VPS (the fixed-IP, always-on side).

Accepts exactly one trusted peer (the Mac) authenticated by a shared token.
Owns the authoritative copy of settings; the Mac's changes arrive as
proposals and are applied here via the existing core.database functions,
then the confirmed state is pushed back to every connected peer.

Also implements the STAND_DOWN / RESUME handshake that makes the Local/Remote
toggle safe: STAND_DOWN stops this node's own trading engines (recording
which ones it stopped, so RESUME only restarts what sync itself paused —
never overriding a preference the VPS's own operator set deliberately) and
replies with a summary of currently-open positions.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.cluster.sync import tls_util
from backend.src.services.cluster.sync._telemetry import TelemetryMixin
from backend.src.services.cluster.sync._server_peer_data import ServerPeerDataMixin
from backend.src.services.cluster.sync._expert_params_sync import ServerExpertParamsMixin
from backend.src.services.cluster.sync.synced_settings import SYNCED_SETTINGS_KEYS
from backend.src.services.cluster.sync.protocol import (
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
    MSG_STRATEGY_PARAMS_PROPOSE, MSG_STRATEGY_PARAMS_STATE, MSG_EXPERT_PARAMS_PROPOSE,
    TRADER_LOCAL, TRADER_REMOTE_VPS, make,
)

log = logging.getLogger("sync")

# The heartbeat/liveness intervals moved to _telemetry.py with the loops
# that are their only readers -- see the note there.


_SYNCED_SETTINGS_KEYS = SYNCED_SETTINGS_KEYS


class SyncServer(TelemetryMixin, ServerPeerDataMixin, ServerExpertParamsMixin):
    def __init__(self, main_engine=None, breakout_engine=None,
                 bounce_engine=None, re_engine=None):
        self._main_engine     = main_engine
        self._breakout_engine = breakout_engine
        self._bounce_engine   = bounce_engine
        self._re_engine      = re_engine
        self._clients: set = set()
        self._server_obj = None
        self._token: Optional[str] = None
        self._last_seen_ts: float = 0.0
        self._liveness_alerted = False
        self._last_liveness_alert_sent_ts: float = 0.0

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self, host: str, port: int, token: str) -> None:
        """token is a random, high-entropy shared secret (see
        settings.generate_sync_token) — compared directly via
        secrets.compare_digest rather than scrypt, which is designed for
        low-entropy human passwords, not a value nobody ever has to type
        twice. Kept in memory only for the process lifetime; persisted at
        rest via core.secrets (Fernet, key in the OS keychain) so this VPS
        can auto-start headlessly across reboots without re-entry."""
        import websockets
        global _listening
        self._token = token

        # A server already bound to the port (started at boot, or by an
        # earlier press) is stopped first: binding twice failed with 10048 on
        # Windows while the first went on listening (2026-09-25).
        if _listening is not None and _listening is not self:
            await _listening.stop()

        ctx = tls_util.server_ssl_context(host)
        # ping_timeout matches the client's (see sync/client.py) — 60s instead
        # of 20s tolerates this app's occasional multi-second event-loop
        # stalls without the ping/pong killing an otherwise-healthy link.
        self._server_obj = await websockets.serve(
            self._handle_connection, "0.0.0.0", port, ssl=ctx,
            ping_interval=20, ping_timeout=60,
        )
        self._tasks = [
            asyncio.create_task(self._heartbeat_loop()),
            asyncio.create_task(self._signal_gen_stats_loop()),
            asyncio.create_task(self._liveness_watchdog_loop()),
        ]
        _listening = self
        log.info("[SyncServer] listening on 0.0.0.0:%d (fingerprint %s)",
                  port, tls_util.cert_fingerprint())

    async def stop(self) -> None:
        """Close the port and cancel this server's loops, which otherwise kept
        heart-beating and could still send "Mac unreachable" alerts."""
        global _listening
        for task in getattr(self, "_tasks", []):
            task.cancel()
        self._tasks = []
        if self._server_obj:
            self._server_obj.close()
            await self._server_obj.wait_closed()
            self._server_obj = None
        if _listening is self:
            _listening = None

    @property
    def is_listening(self) -> bool:
        return getattr(self, "_server_obj", None) is not None

    def set_token(self, token: str) -> None:
        """A new pairing token, effective from the next handshake."""
        self._token = token

    def _check_token(self, token: str) -> bool:
        import secrets as _secrets
        if not self._token or not token:
            return False
        return _secrets.compare_digest(token, self._token)

    # ── Connection handling ──────────────────────────────────────────────────

    async def _handle_connection(self, ws) -> None:
        peer = getattr(ws, "remote_address", ("?", 0))
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=10.0)
            msg = json.loads(raw)
            if msg.get("type") != MSG_HELLO or not self._check_token(msg.get("token", "")):
                await ws.send(json.dumps(make(MSG_REJECT, reason="bad token")))
                await ws.close()
                log.warning("[SyncServer] rejected connection from %s (bad token)", peer)
                return
        except Exception as e:
            log.warning("[SyncServer] handshake failed from %s: %s", peer, e)
            try:
                await ws.close()
            except Exception:
                pass
            return

        self._apply_peer_clock_offset(msg)
        self._clients.add(ws)
        self._last_seen_ts = time.time()
        self._liveness_alerted = False
        log.info("[SyncServer] Mac connected from %s", peer)
        try:
            await ws.send(json.dumps(make(
                MSG_WELCOME,
                settings=self._settings_snapshot(),
                channel_strategy=self._channel_strategy_snapshot(),
                trading_schedule=self._trading_schedule_snapshot(),
                strategy_params=self._strategy_params_snapshot(),
                expert_params=self._expert_params_snapshot(),
                active_trader=db_module.get_active_trader(),
                node_id=db_module.get_or_create_node_id(),
            )))
            upload_buf = bytearray()
            in_upload  = False
            async for raw in ws:
                self._last_seen_ts = time.time()
                self._liveness_alerted = False
                if isinstance(raw, bytes):
                    if in_upload:
                        upload_buf.extend(raw)
                    continue
                msg = json.loads(raw)
                t = msg.get("type")
                if t == MSG_MODEL_SNAPSHOT_UPLOAD:
                    upload_buf = bytearray()
                    in_upload  = True
                    continue
                if t == MSG_MODEL_SNAPSHOT_END:
                    in_upload = False
                    from backend.src.services.cluster.sync.model_transfer import unpack_models, data_dir
                    try:
                        written = unpack_models(bytes(upload_buf), data_dir())
                        log.info("[SyncServer] received model snapshot from Mac: %s", written)
                    except Exception as e:
                        log.error("[SyncServer] failed to unpack uploaded model snapshot: %s", e)
                    upload_buf = bytearray()
                    continue
                await self._dispatch(ws, msg)
        except Exception as e:
            log.info("[SyncServer] connection from %s closed: %s", peer, e)
        finally:
            self._clients.discard(ws)
            # The async-for loop exits on the peer's FIN without necessarily
            # completing our own side of the close handshake — without an
            # explicit close() here the socket is left in CLOSE_WAIT forever.
            # These accumulated over hours (75+ found stuck on this VPS),
            # eventually contributing to new connection attempts timing out.
            try:
                await ws.close()
            except Exception:
                pass

    async def _dispatch(self, ws, msg: dict) -> None:
        t = msg.get("type")
        if t == MSG_PING:
            # Carries the Mac's current UTC offset, so a link that stays up
            # across a clock change still follows it.
            self._apply_peer_clock_offset(msg)
            await ws.send(json.dumps(make(MSG_PONG)))
        elif t == MSG_SETTINGS_PROPOSE:
            await self._handle_settings_propose(ws, msg)
        elif t == MSG_CHANNEL_STRATEGY_PROPOSE:
            await self._handle_channel_strategy_propose(ws, msg)
        elif t == MSG_TRADING_SCHEDULE_PROPOSE:
            await self._handle_trading_schedule_propose(ws, msg)
        elif t == MSG_EXPERT_PARAMS_PROPOSE:
            await self._handle_expert_params_propose(ws, msg)
        elif t == MSG_STRATEGY_PARAMS_PROPOSE:
            await self._handle_strategy_params_propose(ws, msg)
        elif t == MSG_STAND_DOWN:
            await self._handle_stand_down(ws)
        elif t == MSG_RESUME:
            await self._handle_resume(ws)
        elif t == MSG_TRADE_CLOSED:
            self._handle_trade_closed(msg)
        elif t == MSG_LEARNED_RULE_SYNC:
            self._handle_learned_rule_sync(msg)
        elif t == MSG_AI_CONFIG_SYNC:
            self._handle_ai_config_sync(msg)
        elif t == MSG_LEDGER_PULL:
            await self._handle_ledger_pull(ws)
        elif t == MSG_AI_RECOVERED_SIGNAL_SYNC:
            self._handle_ai_recovered_signal_sync(msg)
        elif t == MSG_AI_RECOVERED_PULL:
            await self._handle_ai_recovered_pull(ws)
        elif t == MSG_MODEL_SNAPSHOT_REQUEST:
            await self._handle_model_snapshot_request(ws, msg)
        elif t == MSG_ENGINE_CONTROL:
            await self._handle_engine_control(ws, msg)
        elif t == MSG_MARKET_ORDER:
            await self._handle_market_order(ws, msg)
        elif t == MSG_SIGNAL_ORDER:
            await self._handle_signal_order(ws, msg)
        elif t == MSG_SIGNAL_FOLLOWUP:
            await self._handle_signal_followup(ws, msg)
        else:
            log.debug("[SyncServer] unhandled message type: %s", t)

    async def _handle_engine_control(self, ws, msg: dict) -> None:
        """Start/Stop/Run Now for one of this node's own sub-engines, requested
        by the Mac's Signal Generator panels when it's in Remote mode — those
        buttons would otherwise act on the Mac's own stood-down engine
        instance, which does nothing useful while looking like it worked."""
        engine_name = msg.get("engine", "")
        action      = msg.get("action", "")
        eng = self._sub_engines().get(engine_name)
        error = None
        if eng is None:
            error = f"unknown engine: {engine_name}"
        else:
            try:
                if action == "start":
                    eng.start()
                elif action == "stop":
                    eng.stop()
                elif action == "run_now":
                    await eng._run_cycle()
                elif action == "set_ai_eval":
                    # Bounce/Breakout AI-review toggle — a risk_settings flag,
                    # not an engine lifecycle action, but routed through this
                    # same message type since the Mac's own stood-down copy of
                    # the setting has no effect on which node actually runs
                    # the generator (mirrors the Market Order routing fix).
                    key = {"bounce": "sg_claude_eval_enabled",
                           "breakout": "bo_claude_eval_enabled"}.get(engine_name)
                    if key is None:
                        error = f"set_ai_eval not supported for {engine_name}"
                    else:
                        from backend.src.db import database as _db
                        _db.update_risk_settings({key: 1 if msg.get("enabled") else 0})
                else:
                    error = f"unknown action: {action}"
            except Exception as e:
                error = str(e)
                log.warning("[SyncServer] engine_control %s/%s failed: %s", engine_name, action, e)
        is_running = bool(getattr(eng, "is_running", False)) if eng is not None else False
        payload = {"engine": engine_name, "action": action, "is_running": is_running}
        if error:
            payload["error"] = error
        await ws.send(json.dumps(make(MSG_ENGINE_CONTROL_ACK, **payload)))

    async def _handle_market_order(self, ws, msg: dict) -> None:
        """Place a manual market order on this (VPS) node's own account, on
        behalf of a Mac that's stood down in Remote mode — the Trading tab's
        Market Order button previously just failed there with "Trading stood
        down" instead of actually placing the trade the user asked for."""
        if self._main_engine is None:
            await ws.send(json.dumps(make(
                MSG_MARKET_ORDER_ACK, error="No main engine running on this node",
            )))
            return
        try:
            kwargs = dict(
                direction=msg.get("direction"),
                stop_loss=msg.get("stop_loss"),
                lot_size=msg.get("lot_size"),
                strategy=msg.get("strategy"),
            )
            if msg.get("take_profit") is not None:
                kwargs["take_profit"] = msg["take_profit"]
            if msg.get("source_name") is not None:
                kwargs["source_name"] = msg["source_name"]
            result = await self._main_engine.open_manual_market_order(**kwargs)
            await ws.send(json.dumps(make(MSG_MARKET_ORDER_ACK, result=result)))
        except Exception as e:
            log.warning("[SyncServer] market_order failed: %s", e)
            await ws.send(json.dumps(make(MSG_MARKET_ORDER_ACK, error=str(e))))

    async def _handle_signal_order(self, ws, msg: dict) -> None:
        """Execute a fully-resolved trade forwarded from the Mac's own
        generators (Breakout/TestSignal/REopy/GD2-GD-VIP) under centralized
        signal generation (Settings > Remote Node) — this node has stopped
        analyzing anything itself under that mode (see
        core.database.should_generate_signals_here), so this is the only
        source of new trades while it's active. Calls open_trade() directly,
        not open_trade_from_signal, since the Mac has already resolved risk
        sizing, session gates, and SL/TP logic before sending this.

        open_trade() inserts into vantage_simulated_trades, which has
        FOREIGN KEY (signal_id) REFERENCES vantage_signals(signal_id) — but
        the signal_id here was created in the Mac's own local vantage_signals
        table (by the generator that produced it), never this node's. Every
        forwarded trade failed with "FOREIGN KEY constraint failed" until this
        mirrors a minimal matching row here first, exactly like
        open_manual_market_order() already does for its own synthetic
        signals — confirmed via the VPS log: silent signal_order failures
        recurring for hours (13:20 onward) after centralized mode was
        enabled, each one a missed trade."""
        if self._main_engine is None:
            await ws.send(json.dumps(make(
                MSG_SIGNAL_ORDER_ACK, error="No main engine running on this node",
            )))
            return
        try:
            signal_id = msg.get("signal_id")
            kwargs = dict(
                signal_id=signal_id,
                direction=msg.get("direction"),
                entry_low=msg.get("entry_low"),
                entry_high=msg.get("entry_high"),
                stop_loss=msg.get("stop_loss"),
                tp1=msg.get("tp1"), tp2=msg.get("tp2"), tp3=msg.get("tp3"),
                tp4=msg.get("tp4"), tp5=msg.get("tp5"), tp6=msg.get("tp6"),
                tp7=msg.get("tp7"), tp8=msg.get("tp8"),
                lot_size=msg.get("lot_size", 0.01),
                strategy=msg.get("strategy"),
                tg_source=msg.get("tg_source"),
            )
            from backend.src.services.cluster import sync_repo as _sync_repo
            _sync_repo.mirror_insert_signal_if_absent(signal_id, kwargs)
            result = await self._main_engine.open_trade(**kwargs)
            await ws.send(json.dumps(make(MSG_SIGNAL_ORDER_ACK, result=result)))
            # This node is the one that actually placed the trade, so its own
            # _node_label() ("Remote") is accurate here — the Mac-side caller
            # deliberately skips sending its own commentary/notification for
            # a forwarded trade (see open_trade_from_signal /
            # open_manual_market_order in core/engine.py) since the trade_id
            # only exists in this node's DB.
            try:
                tick = await self._main_engine.get_fresh_tick()
                import asyncio as _asyncio
                _asyncio.create_task(self._main_engine.background_open_commentary(
                    result["trade_id"],
                    {"signal_id": signal_id, "direction": kwargs["direction"],
                     "entry_low": kwargs["entry_low"], "entry_high": kwargs["entry_high"],
                     "stop_loss": kwargs["stop_loss"]},
                    tick,
                ))
            except Exception as e:
                log.warning("[SyncServer] failed to schedule commentary for forwarded trade: %s", e)
        except Exception as e:
            log.warning("[SyncServer] signal_order failed: %s", e)
            await ws.send(json.dumps(make(MSG_SIGNAL_ORDER_ACK, error=str(e))))

    async def _handle_signal_followup(self, ws, msg: dict) -> None:
        """Apply a Telegram follow-up signal's SL/TP to an open instant-entry
        trade that this node itself placed on behalf of the Mac's forwarded
        MSG_SIGNAL_ORDER (see open_trade()'s forwarding branch — that trade
        only exists in THIS node's vantage_simulated_trades, never the Mac's,
        which is why the Mac can't resolve this locally). Mirrors the
        same-node match query engine.py itself uses for the IME follow-up
        check. Returns matched=False when no open trade matches, so the
        caller falls through to opening a new (forwarded) trade instead."""
        if self._main_engine is None:
            await ws.send(json.dumps(make(
                MSG_SIGNAL_FOLLOWUP_ACK, matched=False,
                error="No main engine running on this node",
            )))
            return
        try:
            channel_name = msg.get("channel_name")
            direction    = (msg.get("direction") or "").upper()
            updates      = msg.get("updates") or {}
            tg_id        = msg.get("tg_id")
            from backend.src.services.cluster import sync_repo as _sync_repo
            _instant = _sync_repo.find_latest_instant_trade(channel_name)
            if not _instant or _instant.get("direction", "").upper() != direction:
                await ws.send(json.dumps(make(MSG_SIGNAL_FOLLOWUP_ACK, matched=False)))
                return
            await self._main_engine.apply_followup_to_instant_trade(
                _instant, updates, tg_id, channel_name, channel_name,
            )
            await ws.send(json.dumps(make(MSG_SIGNAL_FOLLOWUP_ACK, matched=True)))
        except Exception as e:
            log.warning("[SyncServer] signal_followup failed: %s", e)
            await ws.send(json.dumps(make(MSG_SIGNAL_FOLLOWUP_ACK, matched=False, error=str(e))))

    # ── Settings sync ────────────────────────────────────────────────────────

    def _settings_snapshot(self) -> dict:
        rs = db_module.get_risk_settings()
        return {k: rs.get(k) for k in _SYNCED_SETTINGS_KEYS if k in rs}

    async def _handle_settings_propose(self, ws, msg: dict) -> None:
        proposed = msg.get("updates") or {}
        updates = {k: v for k, v in proposed.items() if k in _SYNCED_SETTINGS_KEYS}
        # Named, so the Mac drops them from its queue instead of re-sending
        # them on every reconnect for ever.
        ignored = sorted(k for k in proposed if k not in _SYNCED_SETTINGS_KEYS)
        if not updates:
            await ws.send(json.dumps(make(
                MSG_SETTINGS_REJECTED, reason="no recognised settings keys in proposal",
                keys=ignored,
            )))
            return
        if ignored:
            await ws.send(json.dumps(make(
                MSG_SETTINGS_REJECTED, reason="not synced between nodes", keys=ignored,
            )))
        try:
            db_module.update_risk_settings(updates, _from_sync=True)
            log.info("[SyncServer] applied settings from Mac: %s", updates)
        except Exception as e:
            await ws.send(json.dumps(make(MSG_SETTINGS_REJECTED, reason=str(e))))
            return
        await self.broadcast_settings()

    async def broadcast_settings(self) -> None:
        """Push the current confirmed settings snapshot to every connected
        Mac. Called both after applying a Mac's proposal (above) and — via
        database._forward_settings_over_sync — whenever this VPS's own local
        UI (e.g. someone RDP'd in) edits a synced setting directly, so a
        VPS-side edit reaches the Mac just as reliably as the reverse."""
        await self._broadcast(make(MSG_SETTINGS_STATE, settings=self._settings_snapshot()))

    # ── Channel strategy override sync ───────────────────────────────────────

    def _channel_strategy_snapshot(self) -> dict:
        return db_module.get_all_channel_strategy_overrides()

    async def _handle_channel_strategy_propose(self, ws, msg: dict) -> None:
        updates = msg.get("updates") or {}
        for source, entry in updates.items():
            try:
                db_module.set_channel_strategy_override(
                    source, entry.get("strategy"), bool(entry.get("auto")), _from_sync=True,
                )
            except Exception as e:
                log.warning("[SyncServer] failed to apply channel strategy for %s: %s", source, e)
        log.info("[SyncServer] applied channel strategy from Mac: %s", updates)
        await self.broadcast_channel_strategy()

    async def broadcast_channel_strategy(self) -> None:
        """Mirror of broadcast_settings() for per-channel strategy overrides —
        pushed after applying a Mac's proposal and whenever this VPS's own
        local UI changes a channel's strategy directly."""
        await self._broadcast(make(
            MSG_CHANNEL_STRATEGY_STATE, channel_strategy=self._channel_strategy_snapshot()
        ))

    # ── Trading Schedule sync ────────────────────────────────────────────────

    def _trading_schedule_snapshot(self) -> dict:
        from backend.src.services.risk.schedule import trading_schedule_snapshot
        return trading_schedule_snapshot()

    async def _handle_trading_schedule_propose(self, ws, msg: dict) -> None:
        snapshot = msg.get("trading_schedule") or {}
        try:
            from backend.src.services.risk.schedule import apply_trading_schedule_snapshot
            apply_trading_schedule_snapshot(snapshot)
            log.info("[SyncServer] applied trading schedule from Mac")
        except Exception as e:
            log.warning("[SyncServer] failed to apply trading schedule from Mac: %s", e)
            return
        await self.broadcast_trading_schedule()

    async def broadcast_trading_schedule(self) -> None:
        """Mirror of broadcast_settings() for the Trading Schedule — pushed
        after applying a Mac's proposal and whenever this VPS's own local UI
        changes the schedule directly."""
        await self._broadcast(make(
            MSG_TRADING_SCHEDULE_STATE, trading_schedule=self._trading_schedule_snapshot()
        ))

    # ── Strategy Parameters sync ─────────────────────────────────────────────

    def _strategy_params_snapshot(self) -> dict:
        from backend.src.services.risk.strategy_params import strategy_params_snapshot
        return strategy_params_snapshot()

    async def _handle_strategy_params_propose(self, ws, msg: dict) -> None:
        snapshot = msg.get("strategy_params") or {}
        try:
            from backend.src.services.risk.strategy_params import apply_strategy_params_snapshot
            apply_strategy_params_snapshot(snapshot)
            log.info("[SyncServer] applied strategy params from Mac")
        except Exception as e:
            log.warning("[SyncServer] failed to apply strategy params from Mac: %s", e)
            return
        await self.broadcast_strategy_params()

    async def broadcast_strategy_params(self) -> None:
        """Mirror of broadcast_settings() for Strategy Parameters — pushed
        after applying a Mac's proposal and whenever this VPS's own local UI
        changes a strategy's live values directly."""
        await self._broadcast(make(
            MSG_STRATEGY_PARAMS_STATE, strategy_params=self._strategy_params_snapshot()
        ))

    # ── Stand-down / resume ──────────────────────────────────────────────────

    def _sub_engines(self) -> dict:
        return {
            "breakout": self._breakout_engine,
            "bounce":   self._bounce_engine,
            "reversal_engine":  self._re_engine,
        }

    async def _handle_stand_down(self, ws) -> None:
        stopped = []
        for name, eng in self._sub_engines().items():
            if eng is not None and getattr(eng, "is_running", False):
                eng.stop()
                stopped.append(name)
        # A repeat (the Mac stands the VPS down on every reconnect while it is
        # LOCAL) must not forget what the first one stopped, or RESUME would
        # restart nothing.
        if db_module.get_active_trader() == TRADER_LOCAL:
            stopped = sorted(set(stopped) | set(db_module.get_stood_down_engines()))
        db_module.set_stood_down_engines(stopped)
        db_module.set_active_trader(TRADER_LOCAL)
        log.warning(
            "[SyncServer] STAND_DOWN received — stopped engines %s, "
            "main engine will reject new trades until RESUME", stopped,
        )
        positions = []
        if self._main_engine is not None:
            try:
                positions = self._main_engine.get_open_trades()
            except Exception:
                positions = []
        await ws.send(json.dumps(make(
            MSG_STAND_DOWN_ACK,
            stopped_engines=stopped,
            open_positions=[
                {"trade_id": p.get("trade_id"), "direction": p.get("direction"),
                 "entry_price": p.get("entry_price"), "strategy": p.get("strategy")}
                for p in positions
            ],
        )))

    async def _handle_resume(self, ws) -> None:
        to_restart = db_module.get_stood_down_engines()
        engines = self._sub_engines()
        restarted = []
        for name in to_restart:
            eng = engines.get(name)
            if eng is not None and not getattr(eng, "is_running", False):
                eng.start()
                restarted.append(name)
        db_module.set_stood_down_engines([])
        db_module.set_active_trader(TRADER_REMOTE_VPS)
        log.warning("[SyncServer] RESUME received — restarted engines %s", restarted)
        await ws.send(json.dumps(make(MSG_RESUME_ACK, restarted_engines=restarted)))

    def is_standing_down(self) -> bool:
        """Checked by SimulationEngine.open_trade() as the single gate for
        every trade-opening path — see core/engine.py."""
        return db_module.get_active_trader() == TRADER_LOCAL

    # ── Consolidated ledger ──────────────────────────────────────────────────

    def _handle_trade_closed(self, msg: dict) -> None:
        node_id = msg.get("node_id", "")
        trade   = msg.get("trade", {})
        if node_id and trade.get("trade_id"):
            db_module.record_consolidated_trade(node_id, trade)

    async def _handle_ledger_pull(self, ws) -> None:
        rows = db_module.get_consolidated_trades(days=0)
        await ws.send(json.dumps(make(MSG_LEDGER_PUSH, trades=rows)))

    async def push_own_trade_closed(self, trade: dict) -> None:
        """Call from this node's own close-trade paths so the local closure
        is (a) recorded in this node's own ledger and (b) forwarded live to
        the Mac if connected, instead of waiting for the next periodic pull."""
        node_id = db_module.get_or_create_node_id()
        db_module.record_consolidated_trade(node_id, trade)
        await self._broadcast(make(MSG_TRADE_CLOSED, node_id=node_id, trade=trade))

    # ── Learned parser rules (Telegram > Reader Logic > AI tab) ─────────────

    async def _handle_model_snapshot_request(self, ws, msg: dict) -> None:
        """Mac asked to download this node's models. Stream them as
        begin(manifest) -> binary chunks -> end, mirroring the proven
        pattern from backend.src.services.cluster.remote's app-update distribution."""
        from backend.src.services.cluster.sync.model_transfer import package_models, data_dir
        zip_bytes, names = package_models(data_dir())
        chunk_sz = 32 * 1024
        await ws.send(json.dumps(make(MSG_MODEL_SNAPSHOT_BEGIN, files=names, size=len(zip_bytes))))
        for offset in range(0, len(zip_bytes), chunk_sz):
            await ws.send(zip_bytes[offset: offset + chunk_sz])
        await ws.send(json.dumps(make(MSG_MODEL_SNAPSHOT_END)))
        log.info("[SyncServer] sent model snapshot to Mac: %s", names)

    # ── Heartbeat ────────────────────────────────────────────────────────────

    async def _broadcast(self, payload: dict) -> None:
        if not self._clients:
            return
        raw = json.dumps(payload)
        dead = []
        for ws in self._clients:
            try:
                await ws.send(raw)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self._clients.discard(ws)


_instance: Optional[SyncServer] = None
# The one bound to the port. Separate from _instance on purpose: several
# trading paths read "an instance exists" as "this is the VPS", and a stop
# does not change that until the next start of the app.
_listening: Optional[SyncServer] = None


def is_listening() -> bool:
    return _listening is not None


def get_instance() -> Optional[SyncServer]:
    return _instance


def init(main_engine=None, breakout_engine=None, bounce_engine=None, re_engine=None) -> SyncServer:
    global _instance
    _instance = SyncServer(main_engine, breakout_engine, bounce_engine, re_engine)
    return _instance
