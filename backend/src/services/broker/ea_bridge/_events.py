"""Everything the EA reports back, and what it turns into.

The _dispatch fan-out and its handlers: TP hits, stop moves, closes, pending
fills and cancels, plus the template-leg promotion chain those fills drive.
Mixed into EABridge -- see this package's __init__.

Split out of ea_bridge.py verbatim. The one deliberate change is how
_LEG_ROW_WAIT_S is read; it is commented at the site.

The leg promotion chain is the reason events and legs are one module rather
than two: _on_pending_order_filled hands straight to _promote_leg_fill, and
_on_grid_leg_cancelled is the same story with no fill. Separating them would
put a seam through the middle of one flow.
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Optional

from backend.src.services.broker import repo as broker_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.broker.ea_bridge._ids import leg_label, split_leg_trade_id

log = logging.getLogger(__name__)


def _pkg():
    """The ea_bridge package module.

    Names that tests patch on the package must be read through this at call
    time, not bound into this module at import time -- a `from ... import x`
    here takes a reference the patch never reaches, and the guarded behaviour
    then silently stops being guarded while the test still passes.

    That is not hypothetical. Binding check_trading_schedule directly broke
    both schedule-blocked fill tests in this file's first version: the fills
    went through with the schedule gate closed, and only the assertion on
    close_trade_calls caught it.
    """
    from backend.src.services.broker import ea_bridge
    return ea_bridge


class EventsMixin:
    """EABridge's inbound-event handlers. Not instantiated on its own."""

    async def _dispatch(self, msg: dict) -> None:
        t = msg.get("type")
        if t == "hello":
            log.info("[EABridge] EA hello: account=%s symbol=%s",
                     msg.get("account"), msg.get("symbol"))
            self._check_ea_version(msg)
            asyncio.create_task(self.push_global_config())
            asyncio.create_task(self._restore_pending_orders())
            # Re-adopt live positions too, not just resting orders -- see
            # restore_trade(). Without this every EA reload orphaned every
            # open template trade.
            asyncio.create_task(self._restore_open_trades())
            asyncio.create_task(self._push_panel_context())
            self._ensure_panel_loop()
        elif t == "ping":
            await self._send({"type": "pong"})
        elif t == "panel_action":
            await self._on_panel_action(msg)
        elif t in ("trade_opened", "trade_open_failed",
                   "pending_order_placed", "pending_order_open_failed"):
            cb = getattr(self, "_pending_open_acks", {}).get(msg.get("trade_id"))
            if cb:
                cb(msg)
            elif t == "trade_opened":
                # No waiting ack for this id. An EA Template Anchor leg reports
                # its immediate market fill as an unsolicited "trade_opened"
                # under "<trade_id>-a<N>" (HandleOpenTemplateGrid), while the
                # open_trade() call that started it all is waiting on the
                # un-suffixed parent id -- so this never matched a callback and
                # was silently dropped, leaving the parent row a permanent
                # mt5_ticket=0/entry_price=0 placeholder.
                _base, _kind, _num = split_leg_trade_id(msg.get("trade_id") or "")
                if _kind:
                    await self._promote_leg_fill(
                        msg.get("trade_id"), msg.get("ticket"),
                        float(msg.get("fill_price", 0) or 0),
                    )
                else:
                    log.debug("[EABridge] trade_opened with no waiting ack: %s",
                              msg.get("trade_id"))
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
        elif t == "grid_leg_skipped":
            # The EA declined to place one of a grid's resting legs. Until
            # 2026-08-04 this only reached the terminal's own Experts log,
            # so a grid that placed its anchor and quietly lost its pending
            # leg was indistinguishable from one configured with no legs at
            # all -- the symptom that hid zone-mode's wrong-side skip.
            # WARNING rather than info: a leg the template asked for did
            # not reach the broker, which is always worth seeing.
            log.warning(
                "[EABridge] grid leg %s/%s NOT placed for trade=%s: %s "
                "(leg price %.2f vs base %.2f). wrong_side in zone mode now "
                "means price has left the zone ENTIRELY (a leg merely inside "
                "it is pulled back to the market side instead of skipped); "
                "beyond_sl means the template's grid_step_pts is wider than "
                "the signal's own entry-to-SL distance -- these are raw price "
                "deltas, so 20.0 on gold is $20.",
                msg.get("leg"), msg.get("of"), str(msg.get("trade_id", ""))[:8],
                msg.get("reason"), float(msg.get("price", 0) or 0),
                float(msg.get("base", 0) or 0),
            )
        else:
            log.debug("[EABridge] unhandled message type: %s", t)

    async def _resolve_leg_event(self, msg: dict, event: str) -> tuple:
        """Map an inbound EA event onto the vantage_simulated_trades row it
        belongs to, following EA Template leg suffixes.

        Returns (row, row_trade_id, label, owns_row):
          row           the DB row, or None if there is nothing to map onto
          row_trade_id  the id to write against (the un-suffixed parent for
                        a leg event)
          label         "" for a normal trade, else "Anchor Leg 1"/"Grid Leg 2"
          owns_row      True when the reporting leg IS the broker position
                        this row tracks (its mt5_ticket), so trade state may
                        be written. False for a sibling leg -- one row per
                        template trade means there is nowhere to record a
                        second concurrent position, and writing anyway
                        corrupts the tracked leg's lots/close state.
        """
        trade_id = msg.get("trade_id") or ""
        row = await self._fetch_trade(trade_id)
        if row:
            return (row, trade_id, "", True)
        base, kind, num = split_leg_trade_id(trade_id)
        if not kind:
            log.warning("[EABridge] %s for unknown trade_id=%s", event, trade_id)
            return (None, trade_id, "", False)
        row = await self._fetch_trade(base)
        if not row:
            log.warning("[EABridge] %s for unknown template leg trade_id=%s "
                        "(no parent row %s)", event, trade_id, base)
            return (None, base, leg_label(kind, num), False)
        ev_ticket  = int(msg.get("ticket") or 0)
        row_ticket = int(row.get("mt5_ticket") or 0)
        owns = (row_ticket == 0) or (ev_ticket == 0) or (ev_ticket == row_ticket)
        return (row, base, leg_label(kind, num), owns)

    async def _on_tp_hit(self, msg: dict) -> None:
        from backend.src.services.telegram import alerts as telegram_alerts
        trade_id = msg.get("trade_id")
        tp_num   = int(msg.get("tp_num", 0))
        price    = float(msg.get("price", 0))
        lots     = float(msg.get("lots_closed", 0))
        try:
            trade, trade_id, label, owns = await self._resolve_leg_event(msg, "tp_hit")
            if not trade:
                return
            if not owns:
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_template_leg_note(
                        trade, label, f"TP{tp_num} Hit", [
                            f"MT5 Ticket: {msg.get('ticket')}",
                            f"TP{tp_num} price: ${price:.2f}",
                            f"Lots closed: {lots:.2f}",
                        ],
                    ),
                    trade_id, f"tp{tp_num}_hit_sibling_leg",
                ))
                return
            res = await self._engine.partial_close_trade(trade_id, lots, price, f"TP{tp_num}")
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_tp_hit(trade, tp_num, price, lots, res.get("partial_pnl", 0)),
                trade_id, f"tp{tp_num}_hit",
            ))
        except Exception as e:
            log.warning("[EABridge] tp_hit handling failed for %s: %s", msg.get("trade_id"), e)

    async def _on_sl_moved(self, msg: dict) -> None:
        from backend.src.db import database as db_module
        from backend.src.services.telegram import alerts as telegram_alerts
        trade_id = msg.get("trade_id")
        new_sl   = float(msg.get("new_sl", 0))
        # 1-based TP number that triggered this move, 0 if not tied to a
        # specific TP (a continuous trail) — reported by the EA itself since
        # only it knows which tp[] index fired. Previously hardcoded to 0
        # here, which displayed as the misleading "TP0 cleared" on every
        # EA-reported breakeven lock (confirmed live on ticket 1556988985).
        tp_cleared_num = int(msg.get("tp_cleared_num", 0) or 0)
        try:
            trade, trade_id, label, owns = await self._resolve_leg_event(msg, "sl_moved")
            if not trade:
                return
            if not owns:
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_template_leg_note(
                        trade, label, "SL Moved", [
                            f"MT5 Ticket: {msg.get('ticket')}",
                            f"New SL: ${new_sl:.2f}",
                        ],
                    ),
                    trade_id, "sl_moved_ea_sibling_leg",
                ))
                return
            from backend.src.services.broker import repo as broker_repo
            await db_module.to_db_thread(broker_repo.set_stop_loss_be, trade_id, new_sl)
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
        from backend.src.services.telegram import alerts
        try:
            trade, trade_id, label, owns = await self._resolve_leg_event(msg, "trade_closed")
            if not trade:
                return
            if not owns:
                asyncio.create_task(telegram_alerts.send_message(
                    telegram_alerts.fmt_template_leg_note(
                        trade, label, f"Closed ({reason})", [
                            f"MT5 Ticket: {msg.get('ticket')}",
                            f"Close price: ${close_price:.2f}",
                        ],
                    ),
                    trade_id, "ea_close_sibling_leg",
                ))
                return
            # A leg's close IS this trade's close when the leg owns the row's
            # ticket -- record it against the parent id, never the suffixed
            # one (which has no row of its own and previously made the whole
            # event a no-op, leaving the trade permanently "open" in the UI).
            result = await self._engine.record_close(trade_id, close_price, reason)
            account = await self._engine.get_mt5_account()
            closed_row = await self._fetch_trade(trade_id)
            asyncio.create_task(telegram_alerts.send_message(
                telegram_alerts.fmt_trade_close(closed_row, result, {}, account),
                trade_id, "ea_close",
            ))
            if int(closed_row.get("mt5_ticket") or 0):
                # Replace the entry-vs-exit estimate with the broker's own
                # realised figure once the deal history settles.
                # Through the engine's PUBLIC facade: upstream called
                # _schedule_profit_sync, and a service reaching into a runtime
                # private is what tests/core/test_runtime_facade.py stops. The
                # method was promoted and allowlisted instead, which is the
                # route that test names.
                asyncio.create_task(self._engine.schedule_profit_sync(
                    trade_id, int(closed_row["mt5_ticket"]),
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
        from backend.src.db import database as db_module
        from backend.src.services.telegram import alerts as telegram_alerts
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
                if split_leg_trade_id(trade_id)[1]:
                    await self._promote_leg_fill(trade_id, ticket, fill_price)
                else:
                    log.warning("[EABridge] pending_order_filled for unknown trade_id=%s", trade_id)
                return
            tps = json.loads(row["tps_json"])
            now = time.time()

            from backend.src.services.broker import repo as broker_repo
            await db_module.to_db_thread(
                broker_repo.apply_pending_fill,
                trade_id, row, ticket, fill_price, tps, now,
            )
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
                _sched_ok, _sched_reason = _pkg().check_trading_schedule(source=row["channel_name"])
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

    async def _promote_leg_fill(self, leg_trade_id: str, ticket, fill_price: float) -> None:
        """A leg of an EA Template trade (HandleOpenTemplateGrid in the EA)
        went live at the broker -- leg_trade_id is "<original trade_id>-g<N>"
        for a filled grid limit, or "<original trade_id>-a<N>" for an anchor
        leg's immediate market fill. The EA already has this trade fully in
        its own g_trades[] (isTemplate + tpl* fields copied over) and manages
        it correctly regardless of what happens here; this only updates
        Python's own record so the trade shows up as a real, trackable row
        instead of the permanent mt5_ticket=0 placeholder open_trade() wrote
        at template-open time.

        Anchor legs used to reach nothing at all: their fill arrives as an
        unsolicited "trade_opened" (not "pending_order_filled" -- an anchor
        is a market order, never a resting one) under the suffixed id, which
        matched no open_trade() ack callback and was dropped. The row then
        kept mt5_ticket=0/entry_price=0 for life, so the trade showed a $0
        entry in Active Trades, its EA-reported TP/SL/close events were all
        logged as "unknown trade_id" and discarded, and the Telegram close
        message quoted ticket 0, a $0 entry and a P&L computed from it
        (confirmed live 2026-07-29: -$16086 reported on a real -$15.63 loss).

        Only the first leg to go live can promote the row -- there is one
        vantage_simulated_trades row per template trade, and the SELECT below
        finds it via mt5_ticket=0, which only the not-yet-promoted
        placeholder has. With cancel_pending on (the common case) that's the
        only leg that ever fills anyway. With it off, a later leg's fill is a
        genuine second broker position with no DB row of its own -- reported
        via the same formatter, explicitly marked as an additional leg rather
        than pretending it's the trade's row."""
        from backend.src.db import database as db_module
        original_id, kind, num = split_leg_trade_id(leg_trade_id)
        label = leg_label(kind, num)
        now = time.time()
        # The EA reports no volume with a leg fill, and the row's lot_size is
        # Python's own pre-trade sizing -- for a template trade the EA sizes
        # each leg from the template's own Anchor/Pending Lot instead, so the
        # two genuinely differ (0.04 sized vs 0.03 filled, live 2026-07-29).
        # Take the broker's real volume for the promoted leg when we can read
        # it; keep the existing value if the bridge can't answer.
        lots = await self._leg_position_volume(ticket)

        def _apply():
            return broker_repo.claim_template_leg_fill(
                original_id, ticket, fill_price, lots, kind, now)

        # An anchor leg is a market order: the EA fills it and reports back
        # before open_trade() has INSERTed the row, because that INSERT only
        # happens once the EA's own parent ack returns -- and for a multi-leg
        # template that ack legitimately takes tens of seconds (10.5s observed
        # live on 2026-07-30). So a miss here is expected and temporary.
        #
        # The wait is deliberately NOT done inline: _handle_conn awaits
        # _dispatch directly, so blocking here stalls every subsequent EA
        # message AND _last_seen with it -- an inline 10s wait pushed
        # is_ea_healthy() past its 8s timeout and made three template
        # activations fail with "no healthy EA" on 2026-07-30. Hand the retry
        # to its own task and let the reader loop carry on.
        result = await db_module.to_db_thread(_apply)
        row, is_first = result if result else ({}, False)
        if not row:
            asyncio.create_task(self._promote_leg_when_row_exists(
                _apply, label, leg_trade_id, original_id, ticket, fill_price, lots))
            return
        await self._finish_leg_promotion(
            row, is_first, label, original_id, ticket, fill_price, lots)

    async def _promote_leg_when_row_exists(self, _apply, label: str, leg_trade_id: str,
                                           original_id: str, ticket, fill_price: float,
                                           lots) -> None:
        """Wait for open_trade()'s INSERT, then promote the leg.

        Runs as its own task so the EA reader loop keeps draining messages and
        keeps the heartbeat fresh while this waits -- see the call site.

        The budget covers core_open_trade's own ack timeout (capped at 60s)
        plus room for the INSERT that follows it, since the row cannot appear
        until that ack returns. Overshooting costs nothing here; giving up too
        early leaves the row for core_template_placeholder_repair to adopt on
        its next poll, which works but is slower and noisier.
        """
        from backend.src.db import database as db_module
        deadline = time.time() + _pkg()._LEG_ROW_WAIT_S
        while time.time() < deadline:
            await asyncio.sleep(0.5)
            result = await db_module.to_db_thread(_apply)
            row, is_first = result if result else ({}, False)
            if row:
                await self._finish_leg_promotion(
                    row, is_first, label, original_id, ticket, fill_price, lots)
                return
        log.warning("[EABridge] %s filled (trade_id=%s) but no trade row appeared "
                    "for original trade_id=%s within %.0fs — leaving it to "
                    "TemplateRepair",
                    label, leg_trade_id, original_id, _pkg()._LEG_ROW_WAIT_S)

    async def _finish_leg_promotion(self, row: dict, is_first: bool, label: str,
                                    original_id: str, ticket, fill_price: float,
                                    lots) -> None:
        """Everything that happens once a leg fill has been matched to its row,
        shared by the immediate and the deferred paths."""
        from backend.src.services.telegram import alerts
        log.info("[EABridge] %s live: trade=%s ticket=%s @ %.2f lots=%s (%s)",
                 label, original_id[:8], ticket, fill_price, lots or row.get("lot_size"),
                 "promoted this trade's row" if is_first else "sibling leg, row already promoted")

        if is_first:
            self._active[original_id] = {"ticket": ticket, "strategy": row["strategy"]}

            # Trading Schedule gate -- same reasoning as _on_pending_order_
            # filled's own check: the leg was accepted by the broker before
            # we could know whether the window's profit target would still
            # allow it by fill time, and the only protective action left is
            # an immediate real close. Only meaningful for the promoted row
            # -- a later leg has no DB-tracked ticket this app could close.
            _sched_ok, _sched_reason = _pkg().check_trading_schedule(source=row.get("tg_source") or "")
            if not _sched_ok and self._engine is not None:
                try:
                    await self._engine.close_trade(original_id, "trading_schedule_blocked")
                    asyncio.create_task(telegram_alerts.send_message(
                        f"EA Template {label} filled then immediately closed — {_sched_reason}",
                        original_id, "template_leg_filled_schedule_blocked",
                    ))
                except Exception as e:
                    log.warning("[EABridge] schedule-blocked close failed for %s: %s", original_id, e)
                return

        asyncio.create_task(telegram_alerts.send_message(
            telegram_alerts.fmt_leg_fill(row, label, ticket, fill_price, lots, is_first),
            original_id, "template_leg_filled",
        ))

    async def _leg_position_volume(self, ticket) -> Optional[float]:
        """The broker's own volume for a just-filled leg ticket, or None if it
        can't be read (bridge offline, position already gone). Best-effort
        only -- never blocks promoting the row."""
        try:
            if not ticket or self._engine is None:
                return None
            positions = await self._engine._bridge.get_positions() or []
            for p in positions:
                if int(p.get("ticket", 0) or 0) == int(ticket):
                    vol = round(float(p.get("volume", 0) or 0), 4)
                    return vol or None
        except Exception as e:
            log.debug("[EABridge] leg volume lookup failed for ticket=%s: %s", ticket, e)
        return None

    async def _on_grid_leg_cancelled(self, leg_trade_id: str, reason: str) -> None:
        """Counterpart to _promote_leg_fill for a leg that never fills.

        grid_legs_total (2026-08-03, core_open_trade.py -- the EA's own
        trade_opened ack now carries legs_placed) is the one piece of grid
        shape Python can actually know here; without it, this used to have
        no way to tell "one sibling of several cancelled, others may still
        fill" apart from "every leg this grid ever had is now gone with none
        filled" and always assumed the former -- confirmed live 2026-08-03:
        two single-leg grids (no anchor, price outside the zone at signal
        time) each had their only resting leg expire unfilled and sat in
        Active Trades for 5+ hours at a fabricated ~$16,132 unrealised P&L
        (the (current - 0) * lots arithmetic every $0-entry row produces).

        grid_legs_total is None for a row from a synthetic ack-timeout
        placeholder (core_open_trade.py never guesses a leg count there,
        since the EA may genuinely have placed legs Python never heard
        about) -- this still can't safely close in that case, so it falls
        back to the old surface-and-wait behaviour."""
        from backend.src.services.telegram import alerts
        original_id = split_leg_trade_id(leg_trade_id)[0]
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

        total = row.get("grid_legs_total")
        cancelled = await self._incr_grid_leg_cancelled(original_id)
        # `total == 0` is a confirmed "this grid placed nothing" -- not the
        # same as `total is None` ("unknown, don't touch"). `if total and`
        # treated both identically, which mattered in practice: with
        # core_open_trade.py now refusing to insert a row at all when the
        # EA's ack reports 0 legs placed, this branch of 0 should be
        # unreachable going forward, but keep the check correct regardless.
        if total is not None and cancelled >= int(total):
            row = await self._fetch_trade(original_id)  # re-check post-increment
            if row and row["status"] == "open" and int(row["mt5_ticket"] or 0) == 0:
                await self._close_dead_grid_placeholder(row, reason)
            return

        asyncio.create_task(telegram_alerts.send_message(
            f"EA Template grid leg not filled — {row['direction']} {row.get('tg_source', '')} "
            f"({reason}). Other legs may still be resting; this trade stays open at $0 until "
            f"one fills or you close it manually.",
            original_id, "template_grid_leg_cancelled",
        ))

    async def _incr_grid_leg_cancelled(self, trade_id: str) -> int:
        """Atomically bump grid_legs_cancelled and return the new count."""
        from backend.src.db import database as db_module

        return await db_module.to_db_thread(
            broker_repo.incr_grid_leg_cancelled, trade_id)

    async def _close_dead_grid_placeholder(self, row: dict, reason: str) -> None:
        """Every leg this grid ever placed has now cancelled unfilled -- no
        broker position was ever opened, so close the $0-entry placeholder
        via record_close() rather than leaving it a permanent ghost in
        Active Trades. record_close's own entry_price==0 guard (see
        core_close_trade.py) already stops this from fabricating a P&L
        figure from a zero entry, same as core_template_placeholder_repair
        relies on for its own close path."""
        from backend.src.services.telegram import alerts
        from backend.src.services.trading.close_trade import CloseTradeContext, record_close

        trade_id = row["trade_id"]
        bridge = getattr(self._engine, "_bridge", None) if self._engine is not None else None
        if bridge is None:
            log.warning("[EABridge] grid trade=%s has no filled leg left to wait for, but no "
                        "trading bridge is available to close it via — leaving it open",
                        trade_id[:8])
            return
        try:
            ctx = CloseTradeContext(bridge)
            await record_close(trade_id, 0.0, "no_fill_expired", ctx)
        except Exception as e:
            log.warning("[EABridge] failed to close dead grid placeholder trade=%s: %s",
                        trade_id[:8], e)
            return
        log.warning(
            "[EABridge] grid trade=%s closed — every leg (%s total) cancelled (%s) with none "
            "filled, no broker position was ever opened",
            trade_id[:8], row.get("grid_legs_total"), reason,
        )
        asyncio.create_task(telegram_alerts.send_message(
            f"EA Template grid — every leg for {row['direction']} {row.get('tg_source', '')} "
            f"expired/cancelled with none filled. Closing the placeholder (no position was "
            f"ever opened, no P&L).",
            trade_id, "template_grid_no_fill",
        ))

    async def _on_pending_order_cancelled(self, msg: dict) -> None:
        """A resting Limit Runner order was removed from the broker's book
        without filling — either it expired (expire_minutes elapsed, same
        4h default as the Python-simulated zone-wait signals' own pending
        expiry) or was cancelled manually in the terminal; the EA can't
        reliably distinguish the two from a bare "order gone, no matching
        position" observation, so `reason` is best-effort, not authoritative."""
        from backend.src.db import database as db_module
        from backend.src.services.telegram import alerts as telegram_alerts
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
                if split_leg_trade_id(trade_id)[1]:
                    await self._on_grid_leg_cancelled(trade_id, reason)
                else:
                    log.warning("[EABridge] pending_order_cancelled for unknown trade_id=%s", trade_id)
                return
            now = time.time()

            from backend.src.services.broker import repo as broker_repo
            await db_module.to_db_thread(
                broker_repo.apply_pending_cancelled, trade_id, row["signal_id"], now)
            self._pending_orders.pop(trade_id, None)
            asyncio.create_task(telegram_alerts.send_message(
                f"Limit order not filled — {row['direction']} @ {float(row['price']):.2f} "
                f"{reason} before price reached the zone.",
                trade_id, "pending_order_cancelled",
            ))
        except Exception as e:
            log.warning("[EABridge] pending_order_cancelled handling failed for %s: %s", trade_id, e)
