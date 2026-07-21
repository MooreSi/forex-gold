"""
EA bridge — local TCP link between this app (the "brain": Telegram signal
parsing, ML scoring, decides direction/entry/SL/TP) and a companion MQL5 EA
(the "hands": places the order with real broker-side SL/TP and manages the
trail/partial-close ladder tick-by-tick, inside the MT5 terminal's own
OnTick — no polling, no asyncio-stall exposure, no Python/IPC hop on the
hot path).

Runs identically on the Mac (MT5 under Wine) and the VPS (native MT5) —
both sides only ever talk over 127.0.0.1, so the same protocol and the same
compiled .ex5 work unmodified on either machine.

Newline-delimited JSON, one connection (the EA connects out to this node's
own local Python process; this node never dials out to anything). If the
EA disconnects or stops heartbeating, is_ea_healthy() goes False and
engine.py's existing Python-side management (_monitor_loop's per-strategy
handlers) takes back over for any trade still marked managed_by='ea' — see
_fallback_watchdog_loop() in engine.py. Nothing about a trade is ever left
with no manager at all.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

log = logging.getLogger("ea_bridge")

# Strategies whose per-tick SL/TP/partial-close rules are fixed, deterministic
# point/percentage math — faithfully portable to MQL5 and handed to the EA.
# DPM is deliberately excluded: it continuously recomputes trail distance/BE
# trigger/partial % from live ATR, session, momentum, structural swing levels,
# and a per-trade calibration history read from this app's own SQLite DB —
# there is no MT5-native equivalent of that calibration loop, so DPM trades
# always stay Python-managed regardless of whether the EA is connected.
EA_PORTABLE_STRATEGIES = frozenset({
    "scale_out", "be_runner", "trail_stop", "protected_scale",
    "conservative", "scalp_runner", "conservative_trial",
    "signal_climber", "gd_vip_runner", "no_sl_scale",
    "adaptive_runner", "orb_fixed",
})

_HEARTBEAT_TIMEOUT_S = 8.0   # no EA ping/message within this -> treat as unhealthy
_HOST = "127.0.0.1"


def _resolve_port() -> int:
    try:
        import forex_trader.config as _cfg_module
        return int(_cfg_module.get("ea_bridge_port", 9101))
    except Exception:
        return 9101


_PORT = _resolve_port()


class EABridge:
    def __init__(self, engine):
        self._engine = engine
        self._server: Optional[asyncio.base_events.Server] = None
        self._writer: Optional[asyncio.StreamWriter] = None
        self._last_seen: float = 0.0
        # trade_id -> {"ticket": int, "strategy": str} for trades currently
        # handed to the EA — used to validate/enrich incoming events and by
        # the fallback watchdog to know what needs reclaiming if the EA dies.
        self._active: dict[str, dict] = {}

    # ── Lifecycle ────────────────────────────────────────────────────────────

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_conn, _HOST, _PORT)
        log.info("[EABridge] listening on %s:%d", _HOST, _PORT)

    async def stop(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        if self._server is not None:
            self._server.close()

    def is_ea_healthy(self) -> bool:
        return self._writer is not None and (time.time() - self._last_seen) < _HEARTBEAT_TIMEOUT_S

    def is_strategy_portable(self, strategy: str) -> bool:
        return strategy in EA_PORTABLE_STRATEGIES

    # ── Connection handling ──────────────────────────────────────────────────

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        # Only one EA per node — a second connection replaces the first
        # (covers an EA restart/chart reload without a stale writer lingering).
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:
                pass
        self._writer = writer
        self._last_seen = time.time()
        log.info("[EABridge] EA connected from %s", peer)
        try:
            while True:
                line = await reader.readline()
                if not line:
                    break
                self._last_seen = time.time()
                try:
                    msg = json.loads(line.decode().strip())
                except Exception as e:
                    log.debug("[EABridge] bad message: %s (%s)", line[:200], e)
                    continue
                await self._dispatch(msg)
        except Exception as e:
            log.info("[EABridge] EA connection closed: %s", e)
        finally:
            if self._writer is writer:
                self._writer = None
            try:
                writer.close()
            except Exception:
                pass

    async def _send(self, msg: dict) -> bool:
        # drain() has no timeout of its own — if the EA's TCP connection
        # stalls (e.g. it's slow to read while busy managing an existing
        # trade, or the socket has gone zombie without a clean FIN), this
        # would otherwise block forever with nothing above it timing out.
        # That silently hung every caller (open_trade/update_trade), which
        # in turn hung open_manual_market_order() and left the Market Order
        # button greyed out until the page was refreshed. Bounded here so a
        # stalled EA connection fails fast instead.
        if self._writer is None:
            return False
        try:
            self._writer.write((json.dumps(msg) + "\n").encode())
            await asyncio.wait_for(self._writer.drain(), timeout=3.0)
            return True
        except Exception as e:
            log.warning("[EABridge] send failed: %s", e)
            return False

    # ── Outbound: hand a fully-decided trade to the EA ───────────────────────

    async def open_trade(self, trade_id: str, direction: str, lot_size: float,
                         stop_loss: float, tps: dict[int, float], strategy: str,
                         pcts: Optional[list[float]] = None,
                         be_at_pos: Optional[int] = None,
                         timeout: float = 5.0) -> dict:
        """Ask the EA to place the order and take over its management.
        Returns the trade_opened/trade_open_failed payload. Raises
        TimeoutError if the EA doesn't respond — caller falls back to the
        existing Python/bridge open_trade path in that case.

        pcts/be_at_pos: for the ladder-shaped strategies (signal_climber,
        gd_vip_runner, adaptive_runner) — the per-TP close percentage table
        and the compacted TP position where SL first moves to breakeven.
        Sent over the wire as flat pct1..pct8 + be_at_pos fields (matching
        tp1..tp8's existing pattern — the EA's JSON parser is deliberately
        flat/non-nested, see ForexTraderBridge.mq5's own comment on this).
        Python is the single source of truth for these tables; the EA no
        longer hardcodes its own copy for these three strategies, so a new
        ladder-shaped strategy or a tuning change needs no EA rebuild.
        """
        if not self.is_ea_healthy():
            raise ConnectionError("EA not connected/healthy")
        msg = {
            "type": "open_trade", "trade_id": trade_id, "direction": direction,
            "lot_size": lot_size, "stop_loss": stop_loss, "strategy": strategy,
        }
        for n in range(1, 9):
            if n in tps:
                msg[f"tp{n}"] = tps[n]
        if pcts is not None:
            for i, p in enumerate(pcts, start=1):
                msg[f"pct{i}"] = p
            if be_at_pos is not None:
                msg["be_at_pos"] = be_at_pos
        ack_event = asyncio.Event()
        ack_box: dict = {}

        def _on_ack(payload: dict) -> None:
            ack_box.update(payload)
            ack_event.set()

        self._pending_open_acks = getattr(self, "_pending_open_acks", {})
        self._pending_open_acks[trade_id] = _on_ack
        try:
            if not await self._send(msg):
                raise ConnectionError("EA send failed")
            await asyncio.wait_for(ack_event.wait(), timeout=timeout)
        finally:
            self._pending_open_acks.pop(trade_id, None)

        if ack_box.get("type") == "trade_opened":
            self._active[trade_id] = {
                "ticket": ack_box.get("ticket"), "strategy": strategy,
            }
        return ack_box

    async def update_trade(self, trade_id: str, tps: dict[int, float]) -> bool:
        """Push corrected TP levels to a trade the EA is already managing.

        The EA captures tp[]/hasTp[] once, from the open_trade message, and
        never refreshes them on its own — so an IME instant entry (opened
        with provisional/no TP levels) whose real TP1 arrives a minute later
        via a follow-up signal was previously invisible to the EA forever:
        Python's DB had the corrected tp1, but the EA's own on-tick
        TpCleared() check kept comparing against its stale original value,
        so the partial close at TP1 could never fire. Only meaningful for
        trades this bridge is actively tracking (self._active) — a no-op
        (returns False) for anything Python-managed, where update_signal()'s
        normal DB write is already sufficient since the per-tick handler
        re-reads the trade row fresh every cycle.
        """
        if trade_id not in self._active:
            return False
        msg = {"type": "update_trade", "trade_id": trade_id}
        for n in range(1, 9):
            if n in tps:
                msg[f"tp{n}"] = tps[n]
        return await self._send(msg)

    # ── Inbound events from the EA ────────────────────────────────────────────

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            log.info("[EABridge] EA hello: account=%s symbol=%s",
                     msg.get("account"), msg.get("symbol"))
        elif t == "ping":
            await self._send({"type": "pong"})
        elif t in ("trade_opened", "trade_open_failed"):
            cb = getattr(self, "_pending_open_acks", {}).get(msg.get("trade_id"))
            if cb:
                cb(msg)
        elif t == "tp_hit":
            await self._on_tp_hit(msg)
        elif t == "sl_moved":
            await self._on_sl_moved(msg)
        elif t == "trade_closed":
            await self._on_trade_closed(msg)
        else:
            log.debug("[EABridge] unhandled message type: %s", t)

    async def _on_tp_hit(self, msg: dict) -> None:
        from forex_trader.core import telegram_alerts
        trade_id = msg.get("trade_id")
        tp_num   = int(msg.get("tp_num", 0))
        price    = float(msg.get("price", 0))
        lots     = float(msg.get("lots_closed", 0))
        try:
            trade = await self._fetch_trade(trade_id)
            if not trade:
                log.warning("[EABridge] tp_hit for unknown trade_id=%s", trade_id)
                return
            res = await self._engine.partial_close_trade(trade_id, lots, price, f"TP{tp_num}")
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, tp_num, price, lots, res.get("partial_pnl", 0)),
                trade_id, f"tp{tp_num}_hit",
            ))
        except Exception as e:
            log.warning("[EABridge] tp_hit handling failed for %s: %s", trade_id, e)

    async def _on_sl_moved(self, msg: dict) -> None:
        from forex_trader.core import database as db_module
        from forex_trader.core import telegram_alerts
        trade_id = msg.get("trade_id")
        new_sl   = float(msg.get("new_sl", 0))
        # 1-based TP number that triggered this move, 0 if not tied to a
        # specific TP (a continuous trail) — reported by the EA itself since
        # only it knows which tp[] index fired. Previously hardcoded to 0
        # here, which displayed as the misleading "TP0 cleared" on every
        # EA-reported breakeven lock (confirmed live on ticket 1556988985).
        tp_cleared_num = int(msg.get("tp_cleared_num", 0) or 0)
        try:
            trade = await self._fetch_trade(trade_id)
            if not trade:
                log.warning("[EABridge] sl_moved for unknown trade_id=%s", trade_id)
                return
            def _apply():
                with db_module.db() as conn:
                    conn.execute(
                        "UPDATE vantage_simulated_trades SET stop_loss=?, sl_moved_to_be=1 WHERE trade_id=?",
                        (new_sl, trade_id),
                    )
            await db_module.to_db_thread(_apply)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_sl_moved(trade, tp_cleared_num, new_sl),
                trade_id, "sl_moved_ea",
            ))
        except Exception as e:
            log.warning("[EABridge] sl_moved handling failed for %s: %s", trade_id, e)

    async def _on_trade_closed(self, msg: dict) -> None:
        trade_id    = msg.get("trade_id")
        close_price = float(msg.get("close_price", 0))
        reason      = msg.get("reason", "EA_close")
        try:
            trade = await self._fetch_trade(trade_id)
            if not trade:
                log.warning("[EABridge] trade_closed for unknown trade_id=%s", trade_id)
                return
            result = await self._engine._record_close(trade_id, close_price, reason)
            account = await self._engine.get_mt5_account()
            from forex_trader.core import telegram_alerts
            closed_row = await self._fetch_trade(trade_id)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_trade_close(closed_row, result, {}, account),
                trade_id, "ea_close",
            ))
        except Exception as e:
            log.warning("[EABridge] trade_closed handling failed for %s: %s", trade_id, e)
        finally:
            self._active.pop(trade_id, None)

    async def _fetch_trade(self, trade_id: str) -> Optional[dict]:
        from forex_trader.core import database as db_module
        def _fetch():
            with db_module.db() as conn:
                return db_module.row_to_dict(
                    conn.execute("SELECT * FROM vantage_simulated_trades WHERE trade_id=?", (trade_id,)).fetchone()
                )
        return await db_module.to_db_thread(_fetch)


_instance: Optional[EABridge] = None


def get_instance() -> Optional[EABridge]:
    return _instance


def set_instance(bridge: Optional[EABridge]) -> None:
    global _instance
    _instance = bridge
