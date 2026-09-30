"""The Trend PA engine: trend-following price action on XAUUSD (docs/todo/012).

A deliberately simple engine. Every minute it hands the last CLOSED H4, H1
and M15 candles to `strategy.evaluate`; a setup becomes one virtual trade
filled at the live ask (buy) or bid (sell), with the stop from the setup and
the target at 2R from the price actually paid. Every five seconds open
virtual trades are checked against the tick. A close refits the model.

One virtual trade at a time, matching the replay: overlapping entries ride
the same move and make the sample look bigger than the evidence is.

Real orders are `live_execute.py`'s business alone, behind
`tpa_live_execution` (off by default). Generation follows the same node rule
as Breakout and Reversal: the local node only, never the VPS, and not a node
that centralized generation has moved away from.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from backend.src.services.trend_pa import backtest as bt
from backend.src.services.trend_pa import ml
from backend.src.services.trend_pa import outcome as oc
from backend.src.services.trend_pa import repo
from backend.src.services.trend_pa import stats as ss
from backend.src.services.trend_pa import strategy as st

log = logging.getLogger("trend_pa")

CYCLE_S = 60
OUTCOME_S = 5
MAX_HOLD_S = bt.DEFAULT_MAX_HOLD_S
# Bars requested per timeframe; the newest is still forming and is dropped.
CANDLES = {"H4": 210, "H1": 160, "M15": 70}
BACKTEST_DAYS = 1100

_instance: Optional["TrendPAEngine"] = None


def get_instance() -> Optional["TrendPAEngine"]:
    return _instance


def init(bridge, data_dir: Path) -> "TrendPAEngine":
    global _instance
    if _instance is None:
        repo.init(str(Path(data_dir) / "trend_pa.db"))
        _instance = TrendPAEngine(bridge, model_path=Path(data_dir) / "trend_pa_model.pkl")
    return _instance


async def _generates_here() -> bool:
    """Same gate as Breakout and Reversal: node_roles.engines_generate_here."""
    from backend.src.db import database as db_module
    from backend.src.services.cluster import node_roles
    return bool(await db_module.to_db_thread(node_roles.engines_generate_here))


def _live_settings() -> dict:
    from backend.src.db import database as db_module
    return db_module.get_risk_settings()


class TrendPAEngine:
    def __init__(self, bridge, model_path: Path):
        self._bridge = bridge
        self._model_path = Path(model_path)
        self.model = ml.Model.load(self._model_path)
        self.is_running = False
        self.status = "stopped"
        self.status_detail = ""
        self.last_cycle_at: Optional[float] = None
        # The panel's proof of life. `generating_here` is None until the first
        # cycle, then whether this node analyses at all (the other one does
        # when it is False); `last_evaluated_at` is when the strategy last
        # looked at real candles, so a healthy node that finds no setup can be
        # told apart from a stalled one.
        self.generating_here: Optional[bool] = None
        self.last_evaluated_at: Optional[float] = None
        self.backtest_running = False
        self._main_engine = None
        self._tasks: list = []
        self._last_bar_ts = None

    # ── lifecycle ────────────────────────────────────────────────────────────

    def set_main_engine(self, engine) -> None:
        self._main_engine = engine

    def add_refresh_callback(self, cb) -> None:
        """Present for parity with the other engines; the React panel polls."""

    def start(self) -> None:
        if self.is_running:
            return
        repo.set_config("user_stopped", "0")
        self.is_running, self.status = True, "running"
        self._tasks = [asyncio.ensure_future(self._loop(self._run_cycle, CYCLE_S)),
                       asyncio.ensure_future(self._loop(self._check_outcomes, OUTCOME_S))]
        # A fresh install has no evidence at all; the replay gives the panel
        # something honest to show on day one and the model rows to learn from.
        if not repo.closed_signals(origin="backtest"):
            self._tasks.append(asyncio.ensure_future(self.run_backtest()))
        log.info("[TPA] started")

    def stop(self, persist: bool = True) -> None:
        """Stop. `persist=False` is a stand-down (the other node took over),
        which must not be remembered as the owner's choice."""
        if persist:
            repo.set_config("user_stopped", "1")
        self.is_running, self.status = False, "stopped"
        for t in self._tasks:
            if not t.done():
                t.cancel()
        self._tasks = []
        log.info("[TPA] stopped (persist=%s)", persist)

    async def _loop(self, fn, every: float) -> None:
        while self.is_running:
            try:
                await fn()
            except asyncio.CancelledError:
                break
            except Exception as e:
                log.exception("[TPA] %s failed: %s", fn.__name__, e)
                self.status_detail = f"Error: {e}"
            await asyncio.sleep(every)

    # ── generating ───────────────────────────────────────────────────────────

    async def _run_cycle(self) -> None:
        self.last_cycle_at = time.time()
        self.generating_here = await _generates_here()
        if not self.generating_here:
            self.status_detail = "Generation runs on the local node only"
            return
        bars = {}
        for tf, n in CANDLES.items():
            got = await self._bridge.get_candles(tf, n)
            if not got or len(got) < 2:
                self.status_detail = "No market data"
                return
            bars[tf] = got[:-1]          # the newest bar is still forming
        bar_ts = bars["M15"][-1]["ts"]
        new_bar = bar_ts != self._last_bar_ts
        self._last_bar_ts = bar_ts

        self.last_evaluated_at = time.time()
        setup = st.evaluate(bars["H4"], bars["H1"], bars["M15"], datetime.now(timezone.utc))
        if isinstance(setup, str):
            self.status_detail = setup
            if new_bar:
                repo.log_analysis(setup)
            return
        if repo.open_signals():
            self.status_detail = "setup found; one trade already open"
            return
        close_at = float(bar_ts) + bt.M15_S - bt.BROKER_OFFSET_S
        if (repo.last_signal_time(setup.direction) or 0) >= close_at:
            return                        # this bar has already been traded
        await self._open(setup, created_at=max(time.time(), close_at))

    async def _open(self, setup: st.Setup, created_at: float) -> None:
        tick = await self._bridge.get_tick()
        if not tick:
            return
        buy = setup.direction == "BUY"
        entry = float(tick.ask if buy else tick.bid)
        risk = (entry - setup.stop_loss) if buy else (setup.stop_loss - entry)
        if risk <= 0 or risk > st.DEFAULTS["max_sl_atr"] * setup.atr_m15:
            repo.log_analysis(f"{setup.pattern} lost to the spread: stop {risk:.2f} from the fill")
            return
        rr = st.DEFAULTS["rr"]
        sig = {
            "created_at": created_at, "origin": "live", "direction": setup.direction,
            "pattern": setup.pattern, "session": setup.session, "level": setup.level,
            "level_kind": setup.level_kind, "entry": entry, "stop_loss": setup.stop_loss,
            "take_profit": entry + rr * risk if buy else entry - rr * risk,
            "risk": risk, "atr_m15": setup.atr_m15,
            "spread": float(tick.ask) - float(tick.bid),
            "features": setup.features, "ml_prob": self.model.predict(setup.features),
        }
        sig_id = repo.insert_signal(sig)
        self.status_detail = f"{setup.direction} {setup.pattern} at {entry:.2f}"
        log.info("[TPA] TPA-%04d %s %s entry=%.2f sl=%.2f tp=%.2f", sig_id,
                 setup.direction, setup.pattern, entry, sig["stop_loss"], sig["take_profit"])
        if bool(int(_live_settings().get("tpa_live_execution", 0) or 0)):
            from backend.src.services.trend_pa import live_execute
            await live_execute.execute(self, {**sig, "id": sig_id}, tick)

    # ── outcomes ─────────────────────────────────────────────────────────────

    async def _check_outcomes(self, now: Optional[float] = None) -> None:
        open_ = repo.open_signals()
        if not open_:
            return
        tick = await self._bridge.get_tick()
        if not tick:
            return
        now = time.time() if now is None else now
        closed = False
        for s in open_:
            buy = s["direction"] == "BUY"
            result = oc.resolve_tick(s["direction"], s["stop_loss"], s["take_profit"],
                                     float(tick.bid), float(tick.ask))
            if result == "win":
                exit_price = s["take_profit"]
            elif result == "loss":
                exit_price = s["stop_loss"]
            elif now - float(s["created_at"]) >= MAX_HOLD_S:
                result, exit_price = "timeout", float(tick.bid if buy else tick.ask)
            else:
                continue
            # No separate cost: entry was the ask (buy) or bid (sell) and the
            # exit is read off the other side, so the spread is already paid.
            r = oc.r_multiple(s["direction"], s["entry"], exit_price, s["risk"])
            if repo.close_signal(s["id"], result, exit_price, now, r):
                closed = True
                log.info("[TPA] %s closed %s R=%+.2f", s["signal_ref"], result, r)
        if closed:
            self.refit()

    # ── learning and evidence ────────────────────────────────────────────────

    def refit(self) -> dict:
        state = self.model.fit(repo.closed_signals())
        try:
            self.model.save(self._model_path)
        except Exception as e:
            log.warning("[TPA-ML] could not save the model: %s", e)
        return state

    async def run_backtest(self, days: int = BACKTEST_DAYS) -> dict:
        """Replay the strategy over the bridge's history and store the result.
        Reads candles only. Returns the summary."""
        if self.backtest_running:
            return {"error": "a backtest is already running"}
        self.backtest_running = True
        try:
            now = time.time() + 86400
            start = now - days * 86400
            pad = 60 * 86400
            h4 = await self._range("H4", start - pad, now, 400 * 86400)
            h1 = await self._range("H1", start - pad, now, 120 * 86400)
            m15 = await self._range("M15", start, now, 30 * 86400)
            if not m15:
                return {"error": "the bridge returned no history"}
            trades = await asyncio.to_thread(bt.run, h4, h1, m15)
            repo.replace_backtest(trades)
            await asyncio.to_thread(self.refit)
            summary = ss.summarize(trades, rr=st.DEFAULTS["rr"])
            repo.set_config("backtest_at", str(time.time()))
            log.info("[TPA] backtest: %d trades, avg %+.3fR", summary["n"], summary["avg_r"] or 0)
            return {"n": summary["n"], "avg_r": summary["avg_r"]}
        finally:
            self.backtest_running = False

    async def _range(self, tf: str, start: float, end: float, chunk: float) -> list:
        out: dict = {}
        t = start
        while t < end:
            for c in await self._bridge.get_candles_range(t, min(t + chunk, end), tf) or []:
                out[c["ts"]] = c
            t += chunk
        return [out[k] for k in sorted(out)]
