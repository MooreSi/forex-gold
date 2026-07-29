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

from forex_trader.core.core_trading_schedule import check_trading_schedule

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
    "signal_climber", "reversal_runner", "no_sl_scale",
    "adaptive_runner", "adaptive_runner_2", "orb_fixed",
    "limit_runner", "fixed_rr",
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
        # trade_id -> {"ticket": int} for Limit Runner orders currently
        # resting on the broker (placed, not yet filled/cancelled/expired).
        self._pending_orders: dict[str, dict] = {}

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
        # EA Templates (Trading > Strategy > EA Templates, "template:<name>"
        # channel overrides) are always EA-portable by design -- a template
        # IS an EA-native management definition, there's no Python-managed
        # equivalent to fall back to (see core_open_trade.open_trade's
        # template branch, which raises rather than falling through to the
        # Python bridge path when the EA isn't reachable).
        from forex_trader.core.core_ea_templates import is_template_override
        if is_template_override(strategy):
            return True
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
                         trail_mode: Optional[str] = None,
                         template: Optional[dict] = None,
                         zone_low: Optional[float] = None,
                         zone_high: Optional[float] = None,
                         timeout: float = 5.0) -> dict:
        """Ask the EA to place the order and take over its management.
        Returns the trade_opened/trade_open_failed payload. Raises
        TimeoutError if the EA doesn't respond — caller falls back to the
        existing Python/bridge open_trade path in that case.

        pcts/be_at_pos: for the ladder-shaped strategies (signal_climber,
        reversal_runner, adaptive_runner, adaptive_runner_2) — the per-TP
        close percentage table and the compacted TP position where SL
        first moves to breakeven. Sent over the wire as flat pct1..pct8 +
        be_at_pos fields (matching tp1..tp8's existing pattern — the EA's
        JSON parser is deliberately flat/non-nested, see
        ForexTraderBridge.mq5's own comment on this). Python is the single
        source of truth for these tables; the EA no longer hardcodes its
        own copy for these strategies, so a tuning change needs no EA
        rebuild.

        trail_mode: which SL rule to apply to every TP after be_at_pos.
        None/omitted (the default, and what every strategy before Adaptive
        Runner 2 sends) means "trail to the single immediately-previous TP
        price" — the EA's original, still-hardcoded fallback behaviour.
        "midpoint_lag2" means "trail to the midpoint of the two TPs before
        this one" (Adaptive Runner 2) — unlike pcts/be_at_pos, this DOES
        require its own branch in the EA's ManageLadder(), since the rule
        itself (not just its parameters) differs; see
        core_run_tp_ladder.run_tp_ladder's sl_rule parameter for the
        Python-side equivalent, which must stay in lockstep with this.

        template: the full EA Template field dict (core_ea_templates.py)
        for a "template:<name>" strategy -- sent as flat tpl_* fields
        (same flat-JSON convention as pct1..pct8/be_at_pos above) so the
        EA can run Grid/Stealth/Anchor/Trail/BE/Cancel-Pending/Harvest
        management natively, no recompile needed when a template's values
        change. None for every other (built-in) strategy.
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
            if trail_mode is not None:
                msg["trail_mode"] = trail_mode
        if template is not None:
            # Every template field is forwarded generically as tpl_<key>.
            #
            # This used to be a hand-written line per field, which meant a
            # new template setting needed edits in three places (the
            # template schema, here, and the EA's own parser) and an EA
            # recompile before it could do anything. The EA now keeps the
            # raw payload and reads keys on demand with a default (see
            # TplS/TplD/TplI/TplB in ForexTraderBridge.mq5), so a field
            # added to core_ea_templates.DEFAULTS reaches the EA with no
            # change here and no recompile. Keys the EA doesn't understand
            # are carried harmlessly.
            #
            # Bools are coerced to 1/0 rather than sent as native JSON
            # booleans: the EA's minimal parser only understands numbers
            # and strings for flag fields. Confirmed live 2026-07-23 --
            # sending a Python bool silently evaluated false on the EA
            # side (StringToInteger("true") == 0), so harvest and grid
            # cancel-pending never fired regardless of the setting.
            for _k, _v in template.items():
                if _k in ("name", "created_at", "updated_at"):
                    continue          # bookkeeping, not behaviour
                msg[f"tpl_{_k}"] = (1 if _v else 0) if isinstance(_v, bool) else _v
            # Grid mode, zone-spanned staging (2026-07-28) -- the signal's own
            # stated entry zone. When present, HandleOpenTemplateGrid stages
            # the legs ACROSS this zone rather than stepping grid_step_pts
            # away from current price: a "BUY LIMITS 4063/4068 AREA" message
            # is itself already a grid instruction, and its SL sits just
            # beyond the zone, so fixed stepping walks the legs straight
            # through the stop and every one gets broker-rejected as invalid
            # stops. Omitted (and the EA falls back to step-based staging)
            # for any signal with no meaningful zone.
            if (zone_low is not None and zone_high is not None
                    and float(zone_high) > float(zone_low) > 0):
                msg["zone_low"]  = float(zone_low)
                msg["zone_high"] = float(zone_high)
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

    async def place_pending_order(self, trade_id: str, direction: str, price: float,
                                  lot_size: float, stop_loss: float, tps: dict[int, float],
                                  pcts: list[float], be_at_pos: int, strategy: str,
                                  expire_minutes: float = 240.0,
                                  close_full_on_last: bool = True,
                                  trail_mode: Optional[str] = None,
                                  timeout: float = 5.0) -> dict:
        """Ask the EA to place a genuine resting BuyLimit/SellLimit order —
        unlike open_trade(), this does NOT fill immediately, so the returned
        ack only confirms the order was accepted onto the broker's book, not
        that a trade has opened. Raises TimeoutError if the EA doesn't
        respond. There is no Python-bridge fallback for this call (see
        core_limit_order_signal.py) — a raised/failed result here means the
        signal is simply not captured, not "retry a different way."

        The eventual fill is reported later, asynchronously, as an
        unsolicited "pending_order_filled" message (see _dispatch) — MT5
        gives no synchronous fill confirmation for a pending order, only for
        the order's initial acceptance onto the book.

        strategy: which management strategy the EA (and, once filled,
        ManageTrade's dispatch) should treat this trade as — e.g.
        "limit_runner" (ladder-managed, ManageLadder) or "orb_fixed"
        (single-TP close-all, ManageOrbFixed). Required explicitly rather
        than assumed, since a wrong value here silently mismanages the
        trade once it fills — see _on_pending_order_filled, which stores
        this on vantage_pending_orders and reads it back at fill time
        instead of hardcoding a strategy the way earlier versions of this
        method did.

        tps/pcts/be_at_pos: same shapes as open_trade()'s own ladder
        params (flat tp1..tp8/pct1..pct8/be_at_pos fields). Only meaningful
        for ladder-managed strategies (ManageLadder reads t.pcts/t.beAtPos);
        a single-TP strategy like orb_fixed ignores them — ManageOrbFixed
        just closes everything the instant its one TP is touched — so callers
        for those can pass tps={1: target}, pcts=[1.0], be_at_pos=0 as inert
        placeholders.

        close_full_on_last: True (default) means the last TP closes
        everything remaining, same as every other ladder strategy. False —
        sent only when the originating signal had a literal "TP OPEN" line
        (core_limit_order_signal.py) — means the last TP only closes its own
        pcts[] share, leaving the rest open indefinitely with no further TP
        to close it (ManageLadder's own close_full_on_last branch handles
        this the same way core_run_tp_ladder.run_tp_ladder's Python-side
        fallback does). Sent as an int (0/1), matching this EA's minimal
        JSON parser (no native boolean support — see be_at_pos above it).

        trail_mode: same meaning as open_trade()'s own trail_mode param
        (None/omitted = trail to previous TP; "midpoint_lag2" = Adaptive
        Runner 2's rule) — needed because place_pending_order() is now
        reused by more than just Limit Runner (e.g. Reversal Engine's LIMIT
        ORDER toggle can resolve to any strategy, including AR2).
        """
        if not self.is_ea_healthy():
            raise ConnectionError("EA not connected/healthy")
        msg = {
            "type": "place_pending_order", "trade_id": trade_id, "direction": direction,
            "price": price, "lot_size": lot_size, "stop_loss": stop_loss,
            "strategy": strategy, "expire_minutes": expire_minutes,
            "be_at_pos": be_at_pos, "close_full_on_last": int(close_full_on_last),
        }
        if trail_mode is not None:
            msg["trail_mode"] = trail_mode
        for n in range(1, 9):
            if n in tps:
                msg[f"tp{n}"] = tps[n]
        for i, p in enumerate(pcts, start=1):
            msg[f"pct{i}"] = p
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

        if ack_box.get("type") == "pending_order_placed":
            self._pending_orders[trade_id] = {"ticket": ack_box.get("ticket")}
        return ack_box

    async def _on_panel_action(self, msg: dict) -> None:
        """A button was pressed on the EA's on-chart panel.

        The panel holds no authoritative state: it asks for a change, this
        applies it to the SAVED template, and the resulting set_template
        push is what actually moves the panel's display. That ordering is
        the point -- if the chart mutated its own copy, a chart click and
        an app edit could silently diverge, and the EA's copy would quietly
        win until the next restart.

        A "refresh" action carries no change and simply re-pushes, which is
        also how the panel repopulates after an EA reload.
        """
        # Local imports: this module is pulled in early by the engine, and
        # importing database/templates at module scope reintroduces the
        # circular import the rest of this file already avoids the same way.
        from forex_trader.core import core_ea_templates as _et
        from forex_trader.core import database as db_module
        action = (msg.get("action") or "").strip()
        value  = (msg.get("value") or "").strip()
        name   = (msg.get("template") or "").strip()
        if not name:
            log.info("[EABridge] panel_action %r ignored -- no template in context", action)
            return
        try:
            tpl = await db_module.to_db_thread(_et.get_ea_template, name)
            if not tpl:
                log.warning("[EABridge] panel_action for unknown template %r", name)
                return
            if action and action != "refresh":
                if action not in _et.DEFAULTS:
                    log.warning("[EABridge] panel_action unknown field %r", action)
                    return
                # The wire is all strings; coerce to whatever the field is.
                cur = _et.DEFAULTS[action]
                if isinstance(cur, bool):
                    tpl[action] = value not in ("0", "false", "False", "")
                elif isinstance(cur, int):
                    tpl[action] = int(float(value))
                elif isinstance(cur, float):
                    tpl[action] = float(value)
                else:
                    tpl[action] = value
                tpl = await db_module.to_db_thread(_et.save_ea_template, name, tpl)
                log.info("[EABridge] panel set %s=%s on template '%s'", action, value, name)
            await self.push_template(name, tpl)
        except Exception as e:
            log.warning("[EABridge] panel_action %r failed: %s", action, e)

    async def push_template(self, name: str, template: dict) -> bool:
        """Push a template's values to the EA immediately, outside of any
        trade open.

        A template is normally sent with each open_trade/place_pending_order
        call, so an edit only reaches the EA on the NEXT signal. That is
        fine for a change made between sessions, but not for adjusting a
        live setup -- hence the panel's Send button, which calls this.

        Sent as set_template with the same generic tpl_<key> encoding
        open_trade uses, so the EA parses it through exactly the same path
        (and, like open_trade, simply ignores keys it doesn't recognise).
        Best-effort: returns False when the EA isn't connected rather than
        raising, since the values are already saved and will apply on the
        next signal regardless.
        """
        if not self.is_ea_healthy():
            return False
        msg = {"type": "set_template", "template_name": name}
        for k, v in template.items():
            if k in ("name", "created_at", "updated_at"):
                continue
            msg[f"tpl_{k}"] = (1 if v else 0) if isinstance(v, bool) else v
        ok = await self._send(msg)
        if ok:
            log.info("[EABridge] pushed template '%s' to EA (%d fields)",
                     name, len(msg) - 2)
        return ok

    async def restore_pending_order(self, row: dict) -> None:
        """Push one still-'working' vantage_pending_orders row back to the
        EA right after it reconnects (see _dispatch's "hello" handling).

        g_pending[] is pure in-memory state on the EA side with no
        persistence of its own -- any EA restart (recompile, terminal
        restart, a dropped socket that re-triggers OnInit) silently forgets
        every order that was still resting, and CheckPendingOrders() then
        has nothing left to check: it can never again notice that order's
        eventual fill or broker-side expiry. Confirmed live 2026-07-24: 5
        Limit Runner orders sat "pending" in the UI for 16+ hours after
        genuinely expiring on MT5 hours earlier, because whichever EA
        restart happened in between wiped them from tracking with no way
        for either side to notice afterward. Python is the durable source
        of truth for every field here, so pushing it back closes that gap
        regardless of why tracking was lost.

        Fire-and-forget: no ack is awaited. The EA's own reply -- nothing,
        for an order still genuinely resting; pending_order_filled/
        pending_order_cancelled for one that resolved while this EA was
        disconnected -- already routes through the normal _dispatch
        handlers, identical to a live fill/cancel event."""
        tps  = json.loads(row["tps_json"])
        pcts = json.loads(row["pcts_json"])
        msg = {
            "type": "restore_pending_order",
            "trade_id": row["trade_id"],
            "ticket": row["ea_ticket"],
            "direction": row["direction"],
            "lot_size": row["lot_size"],
            "stop_loss": row["stop_loss"],
            "strategy": row["strategy"],
            "be_at_pos": row["be_at_pos"],
            "close_full_on_last": 0 if row.get("tp_open") else 1,
        }
        for n_str, price in tps.items():
            msg[f"tp{n_str}"] = price
        for i, p in enumerate(pcts, start=1):
            msg[f"pct{i}"] = p
        await self._send(msg)

    async def _restore_pending_orders(self) -> None:
        """Called once per EA connection (on "hello") -- restores every
        still-'working' pending order so a prior EA restart can't leave any
        of them permanently untracked. See restore_pending_order()."""
        from forex_trader.core import database as db_module

        def _fetch():
            with db_module.db() as conn:
                return [
                    db_module.row_to_dict(r) for r in conn.execute(
                        "SELECT * FROM vantage_pending_orders WHERE status='working'"
                    ).fetchall()
                ]
        rows = await db_module.to_db_thread(_fetch)
        for row in rows:
            try:
                await self.restore_pending_order(row)
            except Exception as e:
                log.warning("[EABridge] restore_pending_order failed for trade_id=%s: %s",
                            row.get("trade_id"), e)

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

    async def cancel_pending_order(self, trade_id: str, ticket: int, reason: str) -> bool:
        """Pull a still-resting broker-side pending order before it can fill
        blind (core_pending_order_revalidation.py). Fire-and-forget, same as
        update_trade -- the EA deletes the order and reports its own
        pending_order_cancelled event back (_on_pending_order_cancelled),
        carrying `reason` through unchanged, exactly like any other
        cancellation (expiry, manual). That existing handler already does
        the vantage_pending_orders/vantage_signals update and Telegram
        alert, so this call itself does neither.
        """
        return await self._send({
            "type": "cancel_pending_order", "trade_id": trade_id,
            "ticket": ticket, "reason": reason,
        })

    async def push_global_config(self) -> bool:
        """Push Trading > Global Parameters' Harvest setting to the EA --
        called after every save on that card, and once per connection (see
        _dispatch's "hello" handling) since the EA's own copy is pure
        in-memory state with no persistence, same gap restore_pending_order
        closes for pending orders.

        Unlike the old per-template tpl_harvest_enabled/tpl_harvest_threshold
        (sent once, at open_trade time, and only ever applied to that one
        EA-managed trade), this is standing config the EA checks against
        EVERY open position on the symbol each tick — see
        CheckGlobalHarvest() in ForexTraderBridge.mq5 — so it applies
        immediately to trades already open when the setting changes, and to
        Python-managed (non-EA) trades too, not just ones opened after the
        change. Fire-and-forget: no ack expected, same as update_trade.
        """
        from forex_trader.core import database as db_module
        rs = await db_module.to_db_thread(db_module.get_risk_settings)
        msg = {
            "type": "set_global_config",
            "harvest_enabled": 1 if rs.get("global_harvest_enabled", 0) else 0,
            "harvest_threshold": float(rs.get("global_harvest_threshold_usd", 50.0)),
        }
        return await self._send(msg)

    # ── Inbound events from the EA ────────────────────────────────────────────

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            log.info("[EABridge] EA hello: account=%s symbol=%s",
                     msg.get("account"), msg.get("symbol"))
            asyncio.create_task(self.push_global_config())
            asyncio.create_task(self._restore_pending_orders())
        elif t == "ping":
            await self._send({"type": "pong"})
        elif t == "panel_action":
            await self._on_panel_action(msg)
        elif t in ("trade_opened", "trade_open_failed",
                   "pending_order_placed", "pending_order_open_failed"):
            cb = getattr(self, "_pending_open_acks", {}).get(msg.get("trade_id"))
            if cb:
                cb(msg)
        elif t == "tp_hit":
            await self._on_tp_hit(msg)
        elif t == "sl_moved":
            await self._on_sl_moved(msg)
        elif t == "trade_closed":
            await self._on_trade_closed(msg)
        elif t == "pending_order_filled":
            await self._on_pending_order_filled(msg)
        elif t == "pending_order_cancelled":
            await self._on_pending_order_cancelled(msg)
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

    async def _on_pending_order_filled(self, msg: dict) -> None:
        """A resting Limit Runner order has filled — register it as a
        normal EA-managed trade, mirroring exactly what open_trade()'s own
        EA-ack branch does synchronously for a market order
        (core_open_trade.py), just deferred until the real broker fill
        instead of immediate. From here on this trade is indistinguishable
        from any other EA-managed trade — same vantage_simulated_trades
        row shape, same self._active tracking, same fallback-watchdog
        reclaim path if the EA later goes unhealthy."""
        from forex_trader.core import database as db_module
        from forex_trader.core import telegram_alerts
        trade_id   = msg.get("trade_id")
        ticket     = msg.get("ticket")
        fill_price = float(msg.get("fill_price", 0))
        try:
            row = await self._fetch_pending_order(trade_id)
            if not row:
                # EA Template grid legs (core_ea_templates.py / HandleOpenTemplateGrid
                # in the EA) never get a vantage_pending_orders row -- each leg is
                # tracked only in the EA's own g_pending[], keyed "<original
                # trade_id>-g<N>". Fall back to promoting the original open_trade()
                # placeholder row (mt5_ticket=0, entry_price=0.0) instead of
                # dropping the fill silently.
                if "-g" in trade_id:
                    await self._promote_grid_leg_fill(trade_id, ticket, fill_price)
                else:
                    log.warning("[EABridge] pending_order_filled for unknown trade_id=%s", trade_id)
                return
            tps = json.loads(row["tps_json"])
            now = time.time()

            def _apply():
                with db_module.db() as conn:
                    conn.execute(
                        """INSERT INTO vantage_simulated_trades
                           (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                            lot_size,remaining_lots,stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                            status,open_time,spread_cost,commission,slippage_cost,net_pnl,strategy,
                            tg_source,managed_by,tp_open,order_type,pending_placed_at)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (trade_id, row["signal_id"], ticket, row["direction"],
                         row["price"], row["price"], fill_price,
                         row["lot_size"], row["lot_size"], row["stop_loss"],
                         tps.get("1"), tps.get("2"), tps.get("3"), tps.get("4"),
                         tps.get("5"), tps.get("6"), tps.get("7"), tps.get("8"),
                         "open", now, 0.0, 0.0, 0.0, 0.0, row["strategy"],
                         # channel_name was known and stored at placement time
                         # (core_limit_order_signal.py / orb_auto_execute) but
                         # never carried over here -- confirmed live 2026-07-23
                         # that every Limit Runner fill lost its real channel
                         # attribution and showed as an unattributed trade in
                         # Trade Analysis.
                         row["channel_name"], "ea", row["tp_open"],
                         "limit", row["created_at"]),
                    )
                    conn.execute(
                        "UPDATE vantage_signals SET status='active' WHERE signal_id=?",
                        (row["signal_id"],),
                    )
                    conn.execute(
                        "UPDATE vantage_pending_orders SET status='filled',resolved_at=? WHERE trade_id=?",
                        (now, trade_id),
                    )
            await db_module.to_db_thread(_apply)
            self._active[trade_id] = {"ticket": ticket, "strategy": row["strategy"]}
            self._pending_orders.pop(trade_id, None)

            # Trading Schedule gate -- the resting order was accepted by the
            # broker before we could know whether the window's profit target
            # would still allow it by the time it actually filled. ORB/IVB is
            # exempt (its own once-a-day dedup already caps volume, and it's
            # never reached resolve_open_trade_params() by design); every
            # other pending-order strategy (Limit Runner today) must not add
            # risk once the target is hit. The fill already happened -- an
            # immediate real close is the only protective action left.
            if row["strategy"] != "orb_fixed":
                _sched_ok, _sched_reason = check_trading_schedule(source="telegram")
                if not _sched_ok and self._engine is not None:
                    try:
                        await self._engine.close_trade(trade_id, "trading_schedule_blocked")
                        asyncio.create_task(telegram_alerts.send_message(
                            f"Limit order filled then immediately closed — {_sched_reason}",
                            trade_id, "pending_order_filled_schedule_blocked",
                        ))
                    except Exception as e:
                        log.warning("[EABridge] schedule-blocked close failed for %s: %s", trade_id, e)
                    return

            asyncio.create_task(telegram_alerts.send_message(
                f"Limit order FILLED — {row['direction']} {row['lot_size']:g} lots @ "
                f"{fill_price:.2f} (ticket {ticket}), SL {row['stop_loss']:.2f}",
                trade_id, "pending_order_filled",
            ))
        except Exception as e:
            log.warning("[EABridge] pending_order_filled handling failed for %s: %s", trade_id, e)

    async def _promote_grid_leg_fill(self, leg_trade_id: str, ticket, fill_price: float) -> None:
        """A leg of an EA Template grid (HandleOpenTemplateGrid in the EA)
        filled -- leg_trade_id is "<original trade_id>-g<N>". The EA already
        has this trade fully in its own g_trades[] (isTemplate + tpl* fields
        copied over at promotion time) and manages it correctly regardless
        of what happens here; this only updates Python's own record so the
        trade shows up as a real, trackable row instead of the permanent
        mt5_ticket=0 placeholder open_trade() wrote at grid-open time.
        Only the first leg to fill promotes the row -- with cancel_pending
        on (the common case) that's the only leg that ever will; with it
        off, later legs' fills are still managed live by the EA but aren't
        separately reflected here (same single-row shape every other
        strategy uses)."""
        from forex_trader.core import database as db_module
        from forex_trader.core import telegram_alerts
        original_id = leg_trade_id.rsplit("-g", 1)[0]
        now = time.time()

        def _apply():
            with db_module.db() as conn:
                row = db_module.row_to_dict(conn.execute(
                    "SELECT * FROM vantage_simulated_trades WHERE trade_id=? AND mt5_ticket=0",
                    (original_id,),
                ).fetchone())
                if not row:
                    return None
                conn.execute(
                    "UPDATE vantage_simulated_trades SET mt5_ticket=?,entry_price=?,open_time=?,"
                    "order_type='limit',pending_placed_at=? WHERE trade_id=?",
                    # row["open_time"] (read above, before this UPDATE overwrites
                    # it) is when open_trade() placed the grid legs -- the only
                    # placement timestamp that exists for a leg, since grid legs
                    # never get their own vantage_pending_orders row.
                    (ticket, fill_price, now, row["open_time"], original_id),
                )
                return row
        row = await db_module.to_db_thread(_apply)
        if row is None:
            log.warning("[EABridge] grid leg filled (trade_id=%s) but no open placeholder "
                        "row found for original trade_id=%s", leg_trade_id, original_id)
            return
        self._active[original_id] = {"ticket": ticket, "strategy": row["strategy"]}

        # Trading Schedule gate -- same reasoning as _on_pending_order_filled's
        # own check: the leg was accepted by the broker before we could know
        # whether the window's profit target would still allow it by fill
        # time, and the only protective action left is an immediate real close.
        _sched_ok, _sched_reason = check_trading_schedule(source="telegram")
        if not _sched_ok and self._engine is not None:
            try:
                await self._engine.close_trade(original_id, "trading_schedule_blocked")
                asyncio.create_task(telegram_alerts.send_message(
                    f"EA Template grid leg filled then immediately closed — {_sched_reason}",
                    original_id, "template_grid_leg_filled_schedule_blocked",
                ))
            except Exception as e:
                log.warning("[EABridge] schedule-blocked close failed for %s: %s", original_id, e)
            return

        asyncio.create_task(telegram_alerts.send_message(
            f"EA Template grid leg FILLED — {row['direction']} {row['lot_size']:g} lots @ "
            f"{fill_price:.2f} (ticket {ticket})",
            original_id, "template_grid_leg_filled",
        ))

    async def _on_grid_leg_cancelled(self, leg_trade_id: str, reason: str) -> None:
        """Counterpart to _promote_grid_leg_fill for a leg that never fills.
        Python has no record of how many legs a grid opened with or how many
        are still resting (only the EA's own g_pending[] knows that), so this
        can't safely decide "the whole grid is dead" from a single leg's
        cancellation -- another leg may still fill later and promote the row
        exactly as today. What it CAN do is stop the event dead-ending
        silently: surface it so a leg that never fills doesn't sit invisible
        in Active Trades until someone stumbles on it by hand."""
        from forex_trader.core import telegram_alerts
        original_id = leg_trade_id.rsplit("-g", 1)[0]
        row = await self._fetch_trade(original_id)
        if not row:
            log.debug("[EABridge] grid leg %s cancelled (%s) — no placeholder row (already "
                      "closed?)", leg_trade_id, reason)
            return
        if row["status"] != "open" or int(row["mt5_ticket"] or 0) != 0:
            # Another leg already filled and promoted this row, or it's
            # since been closed -- a losing sibling leg cancelling now is
            # expected and harmless.
            return
        log.warning("[EABridge] grid leg %s cancelled (%s) — trade=%s still has no filled "
                    "leg (mt5_ticket=0)", leg_trade_id, reason, original_id[:8])
        asyncio.create_task(telegram_alerts.send_message(
            f"EA Template grid leg not filled — {row['direction']} {row.get('tg_source', '')} "
            f"({reason}). Other legs may still be resting; this trade stays open at $0 until "
            f"one fills or you close it manually.",
            original_id, "template_grid_leg_cancelled",
        ))

    async def _on_pending_order_cancelled(self, msg: dict) -> None:
        """A resting Limit Runner order was removed from the broker's book
        without filling — either it expired (expire_minutes elapsed, same
        4h default as the Python-simulated zone-wait signals' own pending
        expiry) or was cancelled manually in the terminal; the EA can't
        reliably distinguish the two from a bare "order gone, no matching
        position" observation, so `reason` is best-effort, not authoritative."""
        from forex_trader.core import database as db_module
        from forex_trader.core import telegram_alerts
        trade_id = msg.get("trade_id")
        reason   = msg.get("reason", "cancelled")
        try:
            row = await self._fetch_pending_order(trade_id)
            if not row:
                # EA Template grid legs never get a vantage_pending_orders row
                # (see _on_pending_order_filled's identical fallback) -- without
                # this, a leg that never fills leaves the open_trade() placeholder
                # (mt5_ticket=0) permanently invisible: nothing ever updates it,
                # nothing ever alerts, and it sits in Active Trades at $0 until
                # someone notices and cleans it up by hand (confirmed live,
                # trade eb8ca404, sat orphaned 2026-07-28 to 2026-07-29).
                if "-g" in trade_id:
                    await self._on_grid_leg_cancelled(trade_id, reason)
                else:
                    log.warning("[EABridge] pending_order_cancelled for unknown trade_id=%s", trade_id)
                return
            now = time.time()

            def _apply():
                with db_module.db() as conn:
                    conn.execute(
                        "UPDATE vantage_pending_orders SET status='cancelled',resolved_at=? WHERE trade_id=?",
                        (now, trade_id),
                    )
                    conn.execute(
                        "UPDATE vantage_signals SET status='cancelled',cancelled_at=? WHERE signal_id=?",
                        (now, row["signal_id"]),
                    )
            await db_module.to_db_thread(_apply)
            self._pending_orders.pop(trade_id, None)
            asyncio.create_task(telegram_alerts.send_message(
                f"Limit order not filled — {row['direction']} @ {float(row['price']):.2f} "
                f"{reason} before price reached the zone.",
                trade_id, "pending_order_cancelled",
            ))
        except Exception as e:
            log.warning("[EABridge] pending_order_cancelled handling failed for %s: %s", trade_id, e)

    async def _fetch_pending_order(self, trade_id: str) -> Optional[dict]:
        from forex_trader.core import database as db_module
        def _fetch():
            with db_module.db() as conn:
                return db_module.row_to_dict(
                    conn.execute(
                        "SELECT * FROM vantage_pending_orders WHERE trade_id=?", (trade_id,)
                    ).fetchone()
                )
        return await db_module.to_db_thread(_fetch)

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


def get_effective_ea_status() -> tuple[bool, str]:
    """Return (ea_connected, scope_label) for whichever node is actually
    executing trades right now.

    If a paired Local/Remote node is the active trader, this node's own EA
    connection is irrelevant -- it isn't placing any orders -- so this
    reflects the active node's EA state via the sync heartbeat instead
    (SyncServer._status_payload's "ea_connected" field, refreshed every 3s
    on its own). Otherwise (standalone, or this node itself is the active
    trader) this reflects this node's own EABridge connection directly.

    Used by the top-bar EA badge (see ui/app.py) so it stays accurate no
    matter which node is actually doing the trading."""
    try:
        from forex_trader.core import database as db_module
        from forex_trader.sync import client as _sync_cli_mod
        cli = _sync_cli_mod.get_instance()
        if cli is not None and cli.conn_state == "connected" and db_module.get_active_trader() != "local":
            return bool(cli.remote_status.get("ea_connected")), "VPS"
    except Exception:
        pass
    _ea = get_instance()
    return (_ea is not None and _ea.is_ea_healthy()), "this node"
