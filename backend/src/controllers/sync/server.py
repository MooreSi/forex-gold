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

from forex_trader.core import database as db_module
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
    TRADER_LOCAL, TRADER_REMOTE_VPS, make,
)

log = logging.getLogger("sync")

_HEARTBEAT_INTERVAL_S = 3.0
_SIGNAL_GEN_STATS_INTERVAL_S = 15.0
# Comfortably above the 60s ping_timeout (start()) so a genuine drop is
# distinguished from an ordinary stall-tolerant gap between pings, and above
# the reconnect backoff cap (_RECONNECT_BACKOFF_S in sync/client.py) so a
# normal reconnect cycle doesn't false-positive.
_LIVENESS_ALERT_THRESHOLD_S = 90.0
_LIVENESS_CHECK_INTERVAL_S = 30.0
# A flapping Mac connection (brief drop/reconnect cycles, e.g. from its own
# periodic restarts) resets _liveness_alerted on every reconnect, so a series
# of short outages can each independently cross _LIVENESS_ALERT_THRESHOLD_S
# and re-alert — confirmed live: 6+ "Mac unreachable" emails within 20 minutes
# on 2026-07-13 despite the debounce, because each brief reconnect cleared the
# flag before the next drop. This floor caps re-alerts regardless of how many
# separate outage episodes occur, so a flapping link degrades to one alert
# per window instead of spamming the inbox.
_LIVENESS_MIN_REALERT_INTERVAL_S = 600.0


def _get_resource_usage() -> dict:
    """CPU/memory snapshot for this node, sent with every heartbeat so the
    Mac's Remote Node page can show live resource usage for the VPS. Both
    calls are non-blocking C-extension reads (cpu_percent with no interval
    arg returns the delta since the last call) — cheap enough to run on
    every 3s heartbeat with no thread hop needed."""
    try:
        import psutil
        vm = psutil.virtual_memory()
        return {
            "cpu_percent":     psutil.cpu_percent(interval=None),
            "mem_used_mb":     round((vm.total - vm.available) / (1024 * 1024), 1),
            "mem_total_mb":    round(vm.total / (1024 * 1024), 1),
            "mem_percent":     vm.percent,
        }
    except Exception:
        return {}

# Settings columns that are safe to sync — a deliberate allowlist rather than
# "sync every column" so credentials and machine-specific paths (MT5 login,
# terminal_path, etc.) can never leak across the wire even by accident.
_SYNCED_SETTINGS_KEYS = (
    "risk_governor_enabled", "risk_per_trade_pct", "max_risk_per_trade_pct",
    "max_lot_size", "max_daily_loss_pct", "max_total_drawdown_pct",
    "cooldown_after_loss_min", "trade_strategy",
    "session_asia_enabled", "session_london_enabled", "session_ny_enabled",
    "accept_tg_signals", "auto_execute_signals", "exclude_high_risk",
    "bo_live_execution", "bo_claude_eval_enabled", "kelly_sizing_enabled",
    "max_open_trades",
    # sg_claude_eval_enabled (Bounce Generator's own Claude-eval toggle) was
    # missing here entirely — its Breakout Engine sibling (bo_claude_eval_enabled,
    # above) was already synced, but this one wasn't, found investigating a
    # sudden token-usage spike 2026-07-06 traced to both engines' Claude
    # evaluation defaulting ON and running independently on each node.
    "sg_claude_eval_enabled",
    # Circuit breaker fields (settings.py's save_risk()) were never added
    # here when that feature shipped, so every circuit-breaker change from
    # either node was silently filtered out and rejected by the other side
    # — not a timing issue, a deterministic naming gap.
    "circuit_breaker_enabled", "circuit_breaker_losses", "circuit_breaker_cooldown_mins",
    "profit_close_usd",
    # circuit_breaker_active_until / circuit_breaker_consec_losses (the
    # breaker's own runtime state, set by database.py's trip/reset logic
    # rather than the Settings UI) were never added alongside the three
    # circuit_breaker_* config fields above — same "no recognised settings
    # keys in proposal" rejection on every reconnect, found 2026-07-11.
    # Syncing this state (not just the policy) matters here specifically
    # because a trip on the active node must still be honored if failover
    # hands trading to the other node mid-cooldown.
    "circuit_breaker_active_until", "circuit_breaker_consec_losses",
    # Also missing (found investigating a "IME didn't fire" report 2026-07-06 —
    # turned out not to be the actual cause that time, both nodes already
    # agreed, but it's a real latent gap for the next time either side
    # changes it without the other noticing).
    "immediate_market_entry",
    # EA bridge is per-node infrastructure (each node has its own attached
    # MT5 terminal + EA), but the ON/OFF *policy* should be shared like every
    # other risk toggle above — otherwise whichever node becomes active
    # later silently reverts to Python-only management because this node's
    # own DB never learned the setting was turned on elsewhere.
    "ea_bridge_enabled",
    # Same gap, found 2026-07-07: Dynamic Position Management was never
    # added here either, so toggling it on one node silently never reached
    # the other — whichever node takes over trading later would manage
    # positions without DPM even though the user explicitly turned it on.
    "dpm_enabled",
    # Same gap again, found 2026-07-07: Reversal Engine's live-execution
    # toggle was never added here, so every propose from either node was
    # silently rejected outright ("no recognised settings keys in
    # proposal") — the two nodes could show opposite ON/OFF states
    # indefinitely with no error surfaced to the user.
    "re_live_execution",
    # Same gap again, found 2026-07-07: five more fields saved by the same
    # Strategy panel save_strategy() call as trade_strategy/profit_close_usd/
    # exclude_high_risk/kelly_sizing_enabled (all already synced above) were
    # never added here, so the whole settings proposal was rejected outright
    # ("no recognised settings keys in proposal") on every strategy-panel
    # save — trail_stop_sl_pts, trailing_stop_distance and strategy_lot_size
    # feed live SL/TP and lot-size math in engine.py, atr_collapse_threshold
    # gates entries in breakout_signal/engine.py and test_signal/engine.py,
    # and display_strategy_id is the display companion of trade_strategy
    # (set together in engine.py's !strategy command) — a mismatch there
    # leaves the picker on the other node highlighting the wrong strategy
    # even when the executed trade_strategy itself is correct.
    "trail_stop_sl_pts", "trailing_stop_distance", "strategy_lot_size",
    "atr_collapse_threshold", "display_strategy_id",
    # Trading > Global Parameters (2026-07-24) -- strategy_lot_size (above)
    # moved here from Active Strategy; these three are new. Added up front
    # this time rather than found later as a gap, per the recurring pattern
    # every entry above this one already documents.
    "strategy_lot_size_grid", "global_harvest_enabled", "global_harvest_threshold_usd",
    "hour_blocklist_enabled",
    # ORB/IVB Report's auto-execute toggle — whichever node ends up as the
    # active trader is the one whose scheduler actually checks this flag, so
    # toggling it from the other node's UI must reach it or the setting is
    # silently a no-op on the node that matters.
    "orb_auto_execute_enabled",
    # Bounce Generator's live-execution toggle (its sibling toggles,
    # bo_live_execution for Breakout and re_live_execution for Reversal Engine,
    # were both already synced) — same deterministic naming gap as those
    # two, found 2026-07-10 investigating a "no recognised settings keys
    # in proposal" rejection on every Mac reconnect.
    "sg_live_execution",
    # ORB/IVB Report's lot size — same reasoning as orb_auto_execute_enabled
    # above: whichever node executes (manually or via the scheduler) needs
    # the value the user actually set, regardless of which node's UI they
    # set it from.
    "orb_lot_size",
    # Centralized signal generation toggle — whichever node ends up the
    # active trader must agree with the Mac on whether it should be
    # analyzing at all (should_generate_signals_here() reads this locally on
    # each node), so it needs to reach both sides like every other execution
    # -affecting flag above, not just live on whichever node's UI set it.
    "centralized_signal_gen_enabled",
)


class SyncServer:
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
        self._token = token

        ctx = tls_util.server_ssl_context(host)
        # ping_timeout matches the client's (see sync/client.py) — 60s instead
        # of 20s tolerates this app's occasional multi-second event-loop
        # stalls without the ping/pong killing an otherwise-healthy link.
        self._server_obj = await websockets.serve(
            self._handle_connection, "0.0.0.0", port, ssl=ctx,
            ping_interval=20, ping_timeout=60,
        )
        asyncio.create_task(self._heartbeat_loop())
        asyncio.create_task(self._signal_gen_stats_loop())
        asyncio.create_task(self._liveness_watchdog_loop())
        log.info("[SyncServer] listening on 0.0.0.0:%d (fingerprint %s)",
                  port, tls_util.cert_fingerprint())

    async def stop(self) -> None:
        if self._server_obj:
            self._server_obj.close()
            await self._server_obj.wait_closed()

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
                    from backend.src.controllers.sync.model_transfer import unpack_models, data_dir
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
            await ws.send(json.dumps(make(MSG_PONG)))
        elif t == MSG_SETTINGS_PROPOSE:
            await self._handle_settings_propose(ws, msg)
        elif t == MSG_CHANNEL_STRATEGY_PROPOSE:
            await self._handle_channel_strategy_propose(ws, msg)
        elif t == MSG_TRADING_SCHEDULE_PROPOSE:
            await self._handle_trading_schedule_propose(ws, msg)
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
                        from forex_trader.core import database as _db
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
            with db_module.db() as conn:
                exists = conn.execute(
                    "SELECT 1 FROM vantage_signals WHERE signal_id=?", (signal_id,)
                ).fetchone()
                if not exists:
                    now = time.time()
                    conn.execute(
                        """INSERT INTO vantage_signals
                           (signal_id, source_name, direction, entry_low, entry_high,
                            stop_loss, tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8,
                            lot_size, notes, status, created_at, activated_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (signal_id, kwargs["tg_source"] or "centralized_forward",
                         kwargs["direction"], kwargs["entry_low"], kwargs["entry_high"],
                         kwargs["stop_loss"], kwargs["tp1"], kwargs["tp2"], kwargs["tp3"],
                         kwargs["tp4"], kwargs["tp5"], kwargs["tp6"], kwargs["tp7"], kwargs["tp8"],
                         kwargs["lot_size"],
                         "Mirrored from Mac-side generator under centralized signal generation",
                         "active", now, now),
                    )
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
                _asyncio.create_task(self._main_engine._background_open_commentary(
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
            with db_module.db() as conn:
                _irow = conn.execute(
                    "SELECT * FROM vantage_simulated_trades "
                    "WHERE status='open' AND tg_source IN (?,?) "
                    "ORDER BY open_time DESC LIMIT 1",
                    (channel_name, f"instant:{channel_name}"),
                ).fetchone()
                _instant = db_module.row_to_dict(_irow) if _irow else None
            if not _instant or _instant.get("direction", "").upper() != direction:
                await ws.send(json.dumps(make(MSG_SIGNAL_FOLLOWUP_ACK, matched=False)))
                return
            await self._main_engine._apply_followup_to_instant_trade(
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
        updates = {k: v for k, v in (msg.get("updates") or {}).items()
                   if k in _SYNCED_SETTINGS_KEYS}
        if not updates:
            await ws.send(json.dumps(make(
                MSG_SETTINGS_REJECTED, reason="no recognised settings keys in proposal"
            )))
            return
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

    def _handle_learned_rule_sync(self, msg: dict) -> None:
        """A parser rule approved on the Mac — mirror it here so the VPS's
        own independent Telethon session also parses future messages of this
        shape deterministically instead of needing its own AI fallback."""
        channel_name = msg.get("channel_name")
        pattern = msg.get("pattern")
        if not channel_name or not pattern:
            return
        db_module.save_synced_learned_rule(
            channel_name, msg.get("rule_type", "ai_derived_parser"), pattern,
            msg.get("action", "auto_parse"), msg.get("notes", ""),
            msg.get("source_msg_id"),
        )
        log.info("[SyncServer] mirrored learned rule from Mac: %s", channel_name)

    # ── AI-recovered signal review queue (Telegram > Reader Logic > AI tab) ──

    def _handle_ai_recovered_signal_sync(self, msg: dict) -> None:
        """Mirror a create/approve/rule_result/discard event from the Mac so
        this node's own copy of the review queue (and its UI) stays
        identical — see sync/protocol.py's MSG_AI_RECOVERED_SIGNAL_SYNC
        comment for why the queue itself, not just the resulting rule,
        needs to be shared."""
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
        log.info("[SyncServer] mirrored ai_recovered_signal %s from Mac: %s", action, tg_message_id)

    async def _handle_ai_recovered_pull(self, ws) -> None:
        rows = db_module.get_unresolved_ai_recovered_signals()
        await ws.send(json.dumps(make(MSG_AI_RECOVERED_PUSH, signals=rows)))

    async def push_own_ai_recovered_signal(self, action: str, **fields) -> None:
        """Call right after the local mutator (save_ai_recovered_signal,
        mark_ai_recovered_signal_approved, mark_ai_recovered_signal_rule_result,
        discard_ai_recovered_signal) so the Mac's mirrored copy of this row
        stays in lockstep — best-effort, same as push_own_trade_closed(); a
        missed delivery is caught by the periodic full-queue pull instead."""
        await self._broadcast(make(MSG_AI_RECOVERED_SIGNAL_SYNC, action=action, **fields))

    # AI provider config lives in config.yaml (backend.src.config), a
    # completely separate store from vantage_risk_settings — allowlisted
    # here rather than added to _SYNCED_SETTINGS_KEYS above, and includes the
    # DeepSeek key (an explicit user opt-in 2026-07-08) unlike that
    # credential-free mechanism. anthropic_api_key deliberately excluded —
    # the VPS may have its own separately-provisioned Claude key/account and
    # this sync was only asked for to fix DeepSeek not reaching the VPS.
    _AI_CONFIG_SYNC_KEYS = ("ai_provider", "claude_model", "deepseek_model", "deepseek_api_key")

    def _handle_ai_config_sync(self, msg: dict) -> None:
        updates = {k: v for k, v in (msg.get("updates") or {}).items()
                   if k in self._AI_CONFIG_SYNC_KEYS}
        if not updates:
            return
        import backend.src.config as cfg_module
        cfg_module.save_to_yaml(updates)  # also reloads config.py's own module-level cache
        # Mirrors what Settings > AI's save handlers do locally (engine._cfg[...] = ...)
        # — most AI call sites read config.py fresh each call (already covered by the
        # save_to_yaml() reload above), but a few (e.g. the Telegram AI fallback) use
        # this engine's own cached _cfg dict, which save_to_yaml() has no way to reach.
        if self._main_engine is not None:
            for k, v in updates.items():
                self._main_engine._cfg[k] = v
        log.info("[SyncServer] applied AI config from Mac: %s",
                 {k: ("***" if k.endswith("_key") else v) for k, v in updates.items()})

    async def push_own_learned_rule(
        self, channel_name: str, rule_type: str, pattern: str, action: str,
        notes: str, source_msg_id: Optional[str],
    ) -> None:
        """Call after this node's own generate_rule() saves a rule locally,
        to forward it live to the Mac if connected — best-effort, same as
        push_own_trade_closed(); a missed delivery just means the Mac keeps
        needing its own AI fallback for this message shape until it
        independently gets and approves the same extraction."""
        await self._broadcast(make(
            MSG_LEARNED_RULE_SYNC, channel_name=channel_name, rule_type=rule_type,
            pattern=pattern, action=action, notes=notes, source_msg_id=source_msg_id,
        ))

    # ── Model snapshot (manual, on-demand) ───────────────────────────────────

    async def _handle_model_snapshot_request(self, ws, msg: dict) -> None:
        """Mac asked to download this node's models. Stream them as
        begin(manifest) -> binary chunks -> end, mirroring the proven
        pattern from backend.src.controllers.remote's app-update distribution."""
        from backend.src.controllers.sync.model_transfer import package_models, data_dir
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

    async def _status_payload(self) -> dict:
        eng = self._main_engine
        if eng is None:
            return {"ts": time.time()}
        try:
            positions = eng.get_open_trades()
        except Exception:
            positions = []
        balance = equity = None
        try:
            mt5_acc = await eng.get_mt5_account()
            if mt5_acc:
                balance = mt5_acc.get("balance")
                equity  = mt5_acc.get("equity")
        except Exception:
            pass
        open_positions = []
        for p in positions:
            try:
                triggered = sorted(await eng._get_triggered_tps(p["trade_id"]))
            except Exception:
                triggered = []
            row = {
                "trade_id":       p.get("trade_id"),
                "direction":      p.get("direction"),
                "entry_price":    p.get("entry_price"),
                "strategy":       p.get("strategy"),
                "mt5_ticket":     p.get("mt5_ticket"),
                "tg_source":      p.get("tg_source"),
                "stop_loss":      p.get("stop_loss"),
                "lot_size":       p.get("lot_size"),
                "remaining_lots": p.get("remaining_lots"),
                "open_time":      p.get("open_time"),
                "sl_moved_to_be": p.get("sl_moved_to_be"),
                "triggered_tps":  triggered,
            }
            for n in range(1, 9):
                row[f"tp{n}"] = p.get(f"tp{n}")
            open_positions.append(row)

        ea_connected = False
        try:
            from backend.src.services.broker import ea_bridge as _ea_bridge_mod
            _ea = _ea_bridge_mod.get_instance()
            ea_connected = _ea is not None and _ea.is_ea_healthy()
        except Exception:
            pass

        return {
            "ts":             time.time(),
            "balance":        balance,
            "equity":         equity,
            "open_positions": open_positions,
            "engines": {
                name: bool(getattr(e, "is_running", False))
                for name, e in self._sub_engines().items()
            },
            "active_trader": db_module.get_active_trader(),
            "ea_connected":  ea_connected,
            **_get_resource_usage(),
        }

    async def _heartbeat_loop(self) -> None:
        while True:
            try:
                payload = await self._status_payload()
                await self._broadcast(make(MSG_STATUS_HEARTBEAT, **payload))
            except Exception as e:
                log.debug("[SyncServer] heartbeat error: %s", e)
            await asyncio.sleep(_HEARTBEAT_INTERVAL_S)

    # ── Liveness watchdog (centralized signal generation only) ───────────────
    #
    # Under centralized_signal_gen_enabled, this VPS has stopped generating
    # its own signals entirely (should_generate_signals_here() is False here)
    # and depends on the Mac forwarding every trade — so unlike the ordinary
    # STAND_DOWN/RESUME flow, a dropped Mac link here means zero new trades
    # until it's back, not just a missed sync tick. Deliberately alert-only,
    # no auto-fallback to local generation: confirmed as the wanted behavior
    # over auto-resuming generation, which would risk two sources of truth
    # briefly disagreeing during a flappy reconnect.
    async def _liveness_watchdog_loop(self) -> None:
        while True:
            await asyncio.sleep(_LIVENESS_CHECK_INTERVAL_S)
            try:
                rs = await db_module.to_db_thread(db_module.get_risk_settings)
                if not rs.get("centralized_signal_gen_enabled"):
                    continue
                if db_module.get_active_trader() != TRADER_REMOTE_VPS:
                    continue
                if self._last_seen_ts == 0.0:
                    continue  # never connected yet this run — nothing to alert on
                gap = time.time() - self._last_seen_ts
                if gap <= _LIVENESS_ALERT_THRESHOLD_S or self._liveness_alerted:
                    continue
                now = time.time()
                if now - self._last_liveness_alert_sent_ts < _LIVENESS_MIN_REALERT_INTERVAL_S:
                    continue
                self._liveness_alerted = True
                self._last_liveness_alert_sent_ts = now
                msg = (
                    f"*Centralized signal generation: Mac unreachable*\n"
                    f"No message from the Mac in {int(gap)}s — it is the only "
                    f"source of new signals right now and this VPS is not "
                    f"falling back to generating its own. Check the Mac node "
                    f"(Settings > Remote Node) as soon as possible."
                )
                from backend.src.services.telegram import alerts as telegram_alerts
                from backend.src.services.notifications import email_service
                await telegram_alerts.send_message(msg, event_type="sync_liveness")
                try:
                    cfg = db_module.get_email_config()
                    await email_service.send_email(
                        "Centralized signal generation: Mac unreachable",
                        msg.replace("*", "").replace("\n", "<br>"),
                        cfg,
                    )
                except Exception as e:
                    log.warning("[SyncServer] liveness email alert failed: %s", e)
            except Exception as e:
                log.warning("[SyncServer] liveness watchdog error: %s", e)

    # ── Signal-generator stats (Remote-mode mirroring) ───────────────────────
    #
    # Deliberately a separate, slower loop from _heartbeat_loop above — win
    # rates, performance breakdowns and ML learning-panel data only change
    # when a signal closes or a periodic retrain happens, not every 3s, so
    # piggybacking this onto the fast heartbeat would just be wasted work on
    # every tick. Cycle logs are excluded on purpose: they're a live
    # debugging tool, not a progress metric, and mirroring them would add
    # real payload size for no benefit to what this feature is for.

    @staticmethod
    def _breakout_stats() -> dict:
        try:
            from backend.src.services.breakout_signal import breakout_signal_repo as bdb
            from backend.src.services.breakout_signal import adaptive_params as ap
            from backend.src.services.breakout_signal import ml_engine as bo_ml
            return {
                "virtual_balance":        bdb.get_virtual_balance(),
                "max_drawdown":           bdb.get_max_drawdown(),
                "stats":                  bdb.get_stats(),
                "open_signals":           bdb.get_open_signals(),
                "all_signals":            bdb.get_all_signals(limit=80),
                "perf_by_breakout_type":  bdb.get_perf_by_breakout_type(),
                "perf_by_adx_band":       bdb.get_perf_by_adx_band(),
                "perf_by_session":        bdb.get_perf_by_session(),
                "perf_by_bias":           bdb.get_perf_by_bias(),
                "params":                 ap.get_all(),
                "ml_summary":             bo_ml.summary(),
                "ml_metrics":             bo_ml.get_ml_metrics(),
            }
        except Exception as e:
            log.debug("[SyncServer] breakout stats snapshot failed: %s", e)
            return {}

    @staticmethod
    def _bounce_stats() -> dict:
        try:
            from backend.src.services.test_signal import test_signal_repo as tdb
            from backend.src.services.test_signal import adaptive_params as ap
            from backend.src.services.test_signal import ml_engine as ml
            return {
                "virtual_balance":     tdb.get_virtual_balance(),
                "max_drawdown":        tdb.get_max_drawdown(),
                "consecutive_losses":  tdb.get_consecutive_losses(),
                "stats":               tdb.get_stats(),
                "open_signals":        tdb.get_open_signals(),
                "all_signals":         tdb.get_all_signals(limit=100),
                "perf_by_session":     tdb.get_perf_by_session(),
                "perf_by_bias":        tdb.get_perf_by_bias(),
                "perf_by_level_type":  tdb.get_perf_by_level_type(),
                "perf_by_regime":      tdb.get_perf_by_regime(),
                "params":              ap.get_all(),
                "regime_overrides":    ap.get_regime_overrides(),
                "ml_summary":          ml.summary(),
                "ml_metrics":          ml.get_ml_metrics(),
            }
        except Exception as e:
            log.debug("[SyncServer] bounce stats snapshot failed: %s", e)
            return {}

    @staticmethod
    def _reversal_engine_stats() -> dict:
        try:
            from backend.src.services.reversal_engine import reversal_engine_repo as re_db
            from backend.src.services.reversal_engine import ml_engine as re_ml
            return {
                "virtual_balance":      re_db.get_virtual_balance(),
                "max_drawdown":         re_db.get_max_drawdown(),
                "stats":                re_db.get_stats(),
                "correlation_history":  re_db.get_correlation_history(days=7),
                "active_levels":        re_db.get_active_levels(),
                "open_signals":         re_db.get_open_signals(),
                "all_signals":          re_db.get_all_signals(limit=60),
                # These four were missing here (unlike the breakout/bounce
                # snapshots above, which already included their equivalents) —
                # the panel/facade side (remote_stats_facade.py's _DbFacade/
                # _MlFacade) already expected these exact keys, so under Remote
                # mode the Reversal Engine panel's Performance Analytics and "Is it
                # learning?" sections would silently render empty even though
                # the underlying data existed on this node, until this was
                # actually generated on both sides.
                "perf_by_session":      re_db.get_perf_by_session(),
                "perf_by_bias":         re_db.get_perf_by_bias(),
                "perf_by_level_type":   re_db.get_perf_by_level_type(),
                "ml_summary":           re_ml.summary(),
                "ml_metrics":           re_ml.get_ml_metrics(),
            }
        except Exception as e:
            log.debug("[SyncServer] reversal_engine stats snapshot failed: %s", e)
            return {}

    async def _signal_gen_stats_payload(self) -> dict:
        return {
            "breakout": await asyncio.to_thread(self._breakout_stats),
            "bounce":   await asyncio.to_thread(self._bounce_stats),
            "reversal_engine":  await asyncio.to_thread(self._reversal_engine_stats),
        }

    async def _signal_gen_stats_loop(self) -> None:
        while True:
            try:
                payload = await self._signal_gen_stats_payload()
                await self._broadcast(make(MSG_SIGNAL_GEN_STATS, **payload))
            except Exception as e:
                log.debug("[SyncServer] signal-gen stats error: %s", e)
            await asyncio.sleep(_SIGNAL_GEN_STATS_INTERVAL_S)


_instance: Optional[SyncServer] = None


def get_instance() -> Optional[SyncServer]:
    return _instance


def init(main_engine=None, breakout_engine=None, bounce_engine=None, re_engine=None) -> SyncServer:
    global _instance
    _instance = SyncServer(main_engine, breakout_engine, bounce_engine, re_engine)
    return _instance
