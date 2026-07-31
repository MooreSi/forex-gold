"""
TestSignalEngine — runs in the background, generates XAUUSD signals in test mode.

This is the thin orchestrator (task 030): lifecycle (incl. the watchdog's
self-healing start() re-entry, unique to this engine) and the outcome-loop's
routing live here. The M15/M5 signal-generation cycle (_GenerateMixin), TP/
SL/time-stop management + close + live-P&L reconciliation (_ManagementMixin),
the velocity/liquidity-sweep monitor (_VelocityMixin), live order dispatch
(_LiveExecuteMixin), and Claude batch tuning (_LearnMixin) are each their
own file. Replaces engine.py -- see
docs/todo/refactor/test-signal-migration/030-*.md for what moved where and
why. _GenerateMixin was split out in a second pass after the first cut of
this file landed at 1,041 lines, over the 800-LOC ceiling -- _run_cycle
alone was ~550 lines.

Guarantees:
  - Never places any MT5 orders (read-only bridge access)
  - Never sends Telegram messages
  - All output goes to the test database and test_signal.log only
  - Completely isolated from the main SimulationEngine
"""
from __future__ import annotations

import asyncio
import logging
import logging.handlers
import time
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from backend.src.services.test_signal import test_signal_repo as tdb
from backend.src.services.test_signal import ml_engine as ml
from backend.src.services.test_signal.test_signal_learn import _LearnMixin
from backend.src.services.test_signal.test_signal_velocity import _VelocityMixin
from backend.src.services.test_signal.test_signal_manage import _ManagementMixin
from backend.src.services.test_signal.test_signal_live_execute import _LiveExecuteMixin
from backend.src.services.test_signal.test_signal_generate import _GenerateMixin

if TYPE_CHECKING:
    from forex_trader.core.mt5_bridge import MT5BridgeClient

_log = logging.getLogger("test_signal")

_LOG_SETUP_DONE = False


def _setup_log(data_dir: Path) -> None:
    global _LOG_SETUP_DONE
    if _LOG_SETUP_DONE:
        return
    log_path = data_dir / "test_signal.log"
    fh = logging.handlers.TimedRotatingFileHandler(
        log_path, when="midnight", backupCount=14, encoding="utf-8", utc=False,
    )
    fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s — %(message)s"))
    fh.setLevel(logging.DEBUG)
    _log.setLevel(logging.DEBUG)
    _log.addHandler(fh)
    _log.propagate = False
    _LOG_SETUP_DONE = True


# ── Constants ─────────────────────────────────────────────────────────────────

_CYCLE_INTERVAL         = 60
_OUTCOME_INTERVAL       = 5
_SIGNAL_MAX_AGE_H       = 2 / 60
_MIN_ZONE_DWELL         = 2
_MIN_LOT                = 0.01


class TestSignalEngine(_GenerateMixin, _ManagementMixin, _VelocityMixin, _LiveExecuteMixin, _LearnMixin):
    def __init__(self, bridge: "MT5BridgeClient"):
        self._bridge        = bridge
        self._running       = False
        self._cycle_task:    Optional[asyncio.Task] = None
        self._outcome_task:  Optional[asyncio.Task] = None
        self._velocity_task: Optional[asyncio.Task] = None
        self._watchdog_task: Optional[asyncio.Task] = None
        self._last_m15_candle_time: float = 0.0
        self._last_m5_candle_time:  float = 0.0
        self._last_cycle_at:         float = 0.0
        self._status: str = "stopped"
        self._status_detail: str = ""
        self._closed_trade_count: int = 0
        self._refresh_callbacks: list = []
        self._main_engine = None
        self._zone_dwell: dict[int, int] = {}
        self._force_scan:          bool  = False
        self._force_scan_event:    Optional[asyncio.Event] = None
        self._last_emergency_scan: float = 0.0
        self._cached_swing_high:   float = 0.0
        self._cached_swing_low:    float = 0.0
        self._sweep_touch_high:    float = 0.0
        self._sweep_touch_low:     float = 0.0
        self._sweep_touch_time:    float = 0.0
        self._cached_candles:      dict  = {}
        self._last_candle_refresh: float = 0.0

    # ── Lifecycle ─────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            # Self-heal: recreate any tasks that exited while the engine was
            # supposed to be running.
            healed = False
            if self._cycle_task is None or self._cycle_task.done():
                self._cycle_task = asyncio.create_task(self._cycle_loop())
                _log.warning("TestSignalEngine: cycle task was dead — restarted")
                healed = True
            if self._outcome_task is None or self._outcome_task.done():
                self._outcome_task = asyncio.create_task(self._outcome_loop())
                _log.warning("TestSignalEngine: outcome task was dead — restarted")
                healed = True
            if healed:
                self._status = "running"
            return
        self._running = True
        self._status  = "running"
        self._force_scan_event = asyncio.Event()
        try:
            self._closed_trade_count = tdb.get_stats().get("closed", 0)
        except Exception:
            pass
        self._cycle_task    = asyncio.create_task(self._cycle_loop())
        self._outcome_task  = asyncio.create_task(self._outcome_loop())
        self._velocity_task = asyncio.create_task(self._velocity_loop())
        self._watchdog_task = asyncio.create_task(self._watchdog_loop())
        asyncio.create_task(self._reconcile_live_pnl())
        _log.info("TestSignalEngine started (closed_trade_count=%d)", self._closed_trade_count)

    def stop(self) -> None:
        self._running = False
        self._status  = "stopped"
        for t in (self._cycle_task, self._outcome_task, self._velocity_task, self._watchdog_task):
            if t and not t.done():
                t.cancel()
        _log.info("TestSignalEngine stopped")

    @property
    def is_running(self) -> bool:
        """True only when the engine is supposed to run AND at least one task is alive."""
        if not self._running:
            return False
        return (
            (self._cycle_task is not None and not self._cycle_task.done())
            or (self._outcome_task is not None and not self._outcome_task.done())
        )

    @property
    def status(self) -> str:
        return self._status

    @property
    def status_detail(self) -> str:
        return self._status_detail

    @property
    def last_cycle_at(self) -> float:
        return self._last_cycle_at

    def set_main_engine(self, engine) -> None:
        self._main_engine = engine

    def add_refresh_callback(self, cb) -> None:
        self._refresh_callbacks = [cb]

    def _notify_refresh(self) -> None:
        for cb in self._refresh_callbacks:
            try:
                cb()
            except Exception:
                pass

    # ── Watchdog ──────────────────────────────────────────────────────────────

    async def _watchdog_loop(self) -> None:
        """Check every 2 minutes that cycle/outcome tasks are still alive.
        If either exited unexpectedly, restart it without requiring a manual
        stop/start cycle."""
        while self._running:
            await asyncio.sleep(120)
            if not self._running:
                break
            for attr, name, coro_fn in (
                ("_cycle_task",    "cycle",    self._cycle_loop),
                ("_outcome_task",  "outcome",  self._outcome_loop),
                ("_velocity_task", "velocity", self._velocity_loop),
            ):
                task: Optional[asyncio.Task] = getattr(self, attr)
                if task is not None and task.done():
                    exc = None
                    try:
                        exc = task.exception()
                    except (asyncio.CancelledError, asyncio.InvalidStateError):
                        pass
                    _log.warning(
                        "TestSignalEngine: %s task exited unexpectedly%s — restarting",
                        name, f" ({exc})" if exc else "",
                    )
                    setattr(self, attr, asyncio.create_task(coro_fn()))

    # ── Analysis cycle ────────────────────────────────────────────────────────

    async def _cycle_loop(self) -> None:
        while self._running:
            try:
                await self._run_cycle()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.error("Cycle error: %s", exc)
                self._status_detail = f"Error: {exc}"
            if self._force_scan_event is not None:
                try:
                    await asyncio.wait_for(
                        self._force_scan_event.wait(), timeout=_CYCLE_INTERVAL
                    )
                except asyncio.TimeoutError:
                    pass
                if self._running:
                    self._force_scan_event.clear()
            else:
                await asyncio.sleep(_CYCLE_INTERVAL)


    # ── Outcome tracking ──────────────────────────────────────────────────────

    async def _outcome_loop(self) -> None:
        while self._running:
            try:
                await self._check_outcomes()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                _log.debug("Outcome loop error: %s", exc)
            await asyncio.sleep(_OUTCOME_INTERVAL)

    async def _check_outcomes(self) -> None:
        open_sigs = tdb.get_open_signals()
        if not open_sigs:
            tdb.expire_old_pending_signals(_SIGNAL_MAX_AGE_H)
            return

        now = time.time()

        try:
            tick = await self._bridge.get_tick()
        except Exception:
            return
        if not tick:
            return

        bid = float(tick.bid)
        ask = float(tick.ask)
        spread_raw = ask - bid
        notify = False

        _sg_live = False
        if self._main_engine is not None:
            try:
                from forex_trader.core import database as _main_db
                _main_rs = _main_db.get_risk_settings()
                _sg_live = bool(_main_rs.get("sg_live_execution", 0))
            except Exception:
                pass

        for sig in open_sigs:
            sid       = sig["id"]
            direction = sig["direction"].upper()

            current = bid if direction == "BUY" else ask

            # ── Live-execution closure sync ───────────────────────────────────
            if sig.get("status") == "triggered" and sig.get("mt5_ticket") and sig.get("live_exec_status") == "success":
                try:
                    from forex_trader.core import database as _mdb

                    def _fetch_mt5_close():
                        import sqlite3 as _sl3
                        with _sl3.connect(f"file:{_mdb._DB_PATH}?mode=ro", uri=True) as _mc:
                            _mc.row_factory = _sl3.Row
                            return _mc.execute(
                                "SELECT status, mt5_profit, net_pnl FROM vantage_simulated_trades "
                                "WHERE mt5_ticket=? AND status='closed'",
                                (sig["mt5_ticket"],),
                            ).fetchone()

                    _mrow = await _mdb.to_db_thread(_fetch_mt5_close)
                    if _mrow:
                        _profit = float(_mrow["mt5_profit"] or _mrow["net_pnl"] or 0)
                        _mt5_outcome = "win" if _profit > 1.0 else ("loss" if _profit < -1.0 else "be")
                        _close_px = current
                        entry_mid = float(sig["entry_mid"] or 0)
                        lot_size  = float(sig.get("lot_size") or _MIN_LOT)
                        _pnl_pts  = round(
                            (_close_px - entry_mid) if direction == "BUY" else (entry_mid - _close_px), 2
                        )
                        _log.info(
                            "[TestSignal] %s closed via MT5 sync: profit=%.2f → %s",
                            sig.get("signal_ref", f"SIG-{sid:04d}"), _profit, _mt5_outcome,
                        )
                        await self._close_signal(
                            sid, _mt5_outcome, _close_px, _pnl_pts, lot_size, direction, spread=spread_raw
                        )
                        notify = True
                        continue
                except Exception as _sync_exc:
                    _log.debug("[TestSignal] MT5 closure sync failed for SIG-%04d: %s", sid, _sync_exc)

            if sig.get("live_exec_status") == "success" and sig.get("status") == "triggered":
                continue

            # ── Trigger pending signal when price enters entry zone ────────────
            if sig["status"] == "pending":
                entry_mid = float(sig["entry_mid"] or 0)
                zone_low  = float(sig["entry_low"]  or entry_mid)
                zone_high = float(sig["entry_high"] or entry_mid)
                atr_m15   = float(sig.get("atr_m15") or 1.0)
                in_zone_buy  = direction == "BUY"  and ask <= zone_high
                in_zone_sell = direction == "SELL" and bid >= zone_low
                in_zone = in_zone_buy or in_zone_sell

                if in_zone:
                    self._zone_dwell[sid] = self._zone_dwell.get(sid, 0) + 1
                    dwell = self._zone_dwell[sid]

                    if dwell < _MIN_ZONE_DWELL:
                        _log.debug(
                            "[TestSignal] SIG-%04d in zone (dwell %d/%d) — waiting",
                            sid, dwell, _MIN_ZONE_DWELL,
                        )
                        continue

                    from forex_trader.core.database import is_session_allowed as _isa
                    _sess_ok, _sess_name = _isa()
                    if not _sess_ok:
                        _log.debug(
                            "[TestSignal] SIG-%04d in zone but session '%s' not enabled — holding",
                            sid, _sess_name,
                        )
                        continue

                    fill_px = ask if direction == "BUY" else bid
                    tdb.update_signal_triggered(sid, fill_px)

                    drift_atr = abs(fill_px - entry_mid) / atr_m15 if atr_m15 > 0 else 0.5
                    tdb.patch_ml_features(sid, {"trigger_drift_atr": round(drift_atr, 4)})

                    self._zone_dwell.pop(sid, None)
                    _log.info(
                        "[TestSignal] SIG-%04d %s triggered @ %.2f (dwell %d cycles, drift %.2f atr)",
                        sid, direction, fill_px, dwell, drift_atr,
                    )
                    if _sg_live:
                        await self._execute_live(sig, fill_px, tick)
                    notify = True
                else:
                    if sid in self._zone_dwell:
                        self._zone_dwell.pop(sid)
                continue

            if sig["status"] != "triggered":
                continue

            # Orphan watchdog: live execution was enabled at trigger time but
            # live_exec_status was never written.
            trigger_time = float(sig.get("trigger_time") or 0)
            if (_sg_live
                    and trigger_time > 0
                    and not sig.get("live_exec_status")
                    and (now - trigger_time) > 120):
                tdb.update_live_exec_result(
                    sig["id"], None, None, "failed:orphaned_no_response"
                )
                _log.warning(
                    "[TestSignal] SIG-%04d triggered %ds ago with live mode on but "
                    "live_exec_status never written — marking orphaned",
                    sid, int(now - trigger_time),
                )

            # Delegate the SL/TP1/TP3/time-stop/conservative-override ladder.
            changed = await self._manage_triggered_signal(sig, bid, ask, now, spread_raw)
            if changed:
                notify = True

        if notify:
            self._notify_refresh()

        tdb.expire_old_pending_signals(_SIGNAL_MAX_AGE_H)


# ── Module-level singleton ────────────────────────────────────────────────────

_instance: Optional[TestSignalEngine] = None


def init(bridge: "MT5BridgeClient") -> TestSignalEngine:
    global _instance
    from backend.src.config import USER_DATA_DIR
    _setup_log(USER_DATA_DIR / "data")

    db_path = str(USER_DATA_DIR / "data" / "test_signal.db")
    tdb.init(db_path)

    _log.info("Test signal DB: %s", db_path)

    ml.init(USER_DATA_DIR / "data")

    _instance = TestSignalEngine(bridge)
    return _instance


def get_instance() -> Optional[TestSignalEngine]:
    return _instance
