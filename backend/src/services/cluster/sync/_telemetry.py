"""What the VPS tells the Mac about itself: status, heartbeat, engine stats.

Moved verbatim out of `server.py` -- same methods, same bodies, same names --
to keep that file inside its size budget. `SyncServer` mixes this in, so
`self._clients`, `self._main_engine` and the rest resolve exactly as they did
inline.

One concern: everything the server BROADCASTS. The status payload, the
heartbeat that carries it, the liveness watchdog that notices when the Mac has
gone quiet, and the per-engine signal-generation stats the Remote Node screen
renders. None of it accepts a command or changes any state on this node --
which is why it reads cleanly on its own, and why `server.py` is left holding
only the connection, the handshake and the handlers that actually decide
things.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Optional

from backend.src.db import database as db_module

log = logging.getLogger(__name__)


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


class TelemetryMixin:
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
                triggered = sorted(await eng.get_triggered_tps(p["trade_id"]))
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
