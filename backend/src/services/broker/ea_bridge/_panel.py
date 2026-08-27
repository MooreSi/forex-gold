"""The on-chart panel half of EABridge.

Pushing context, signals and log lines to the EA's panel, and handling the
button presses that come back. Mixed into EABridge -- see this package's
__init__.

Split out of ea_bridge.py verbatim: same methods, same order, same bodies.
The panel talks to the EA over the same socket as everything else, so every
method here still goes through self._send and depends on the connection state
the main class owns.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Optional

from backend.src.services.broker import repo as broker_repo

log = logging.getLogger(__name__)


# panel_action values that place or pull real orders, as opposed to editing a
# template field. Listed explicitly so a typo in an action name can never fall
# through to the generic template-field path and quietly write garbage into a
# saved template.
_ORDER_ACTIONS = frozenset({
    "market_buy", "market_sell", "limit_buy", "limit_sell",
    "close_all", "cancel_limits",
})


class PanelMixin:
    """EABridge's panel methods. Not instantiated on its own -- it reads
    self._engine, self._panel_slot, self._panel_task and self._send, and calls
    back into cancel_pending_order / push_template / is_ea_healthy on the
    combined class."""

    async def _push_panel_context(self) -> None:
        """Give the on-chart panel a template to act on as soon as the EA
        connects.

        Without this the panel starts blank and every click is refused with
        "no template in context" -- it only gained context once a template
        TRADE happened to open, which on a quiet channel could be hours.
        Observed immediately after the first panel deploy (2026-07-29):
        three button presses all rejected.

        Picks the template assigned to the panel's currently selected
        channel, since that is the one whose behaviour is live; falls back
        to any channel-assigned template, then to the most recently edited
        one, so the panel is still usable while a template is being set up.

        Also sends the channel roster (panel_context) so the CH tabs and the
        TELEGRAM / TG CMD lamps have something to show immediately, rather
        than only after the first template trade opens.
        """
        from backend.src.services.broker import ea_templates as _et
        from backend.src.db import database as db_module
        try:
            def _pick() -> Optional[dict]:
                assigned = db_module.get_all_channel_strategy_overrides() or {}
                # get_all_channel_strategy_overrides returns {"strategy",
                # "auto"}; get_all_channel_strategy_settings returns
                # {"strategy_override", ...}. Read both keys so this cannot
                # silently match nothing again if the caller is ever swapped
                # -- it was written against the wrong key originally, which
                # made this branch dead and sent the most-recently-edited
                # template every time regardless of what was assigned.
                names = set()
                for v in assigned.values():
                    ov = v.get("strategy") or v.get("strategy_override") or ""
                    if _et.is_template_override(ov):
                        names.add(_et.template_name_from_override(ov))
                rows = _et.list_ea_templates()
                if not rows:
                    return None
                for r in rows:
                    if r["name"] in names:
                        return r
                return max(rows, key=lambda r: r.get("updated_at") or 0)

            sel = await self._template_for_selected_slot()
            tpl = sel or await db_module.to_db_thread(_pick)
            if tpl:
                await self.push_template(tpl["name"], tpl)
            await self.push_panel_context()
        except Exception as e:
            log.debug("[EABridge] panel context push skipped: %s", e)

    # ── On-chart panel feeds ─────────────────────────────────────────────────
    #
    # The panel is a view plus a remote control, never an authority (see
    # _on_panel_action). These three pushes are the "view" half:
    #
    #   set_template   the left column -- every editable field, already sent
    #                  generically as tpl_<key>, so a field added to
    #                  core_ea_templates.DEFAULTS reaches the panel with no
    #                  protocol change and no EA recompile.
    #   panel_context  the CH tabs and link lamps (core_panel_context).
    #   panel_signal   the right column's ICT criteria and confidence score
    #                  (core_panel_signal). The EA computes the rest of that
    #                  column -- bid/ask/spread/P&L, the M5-D1 trend row, ATR,
    #                  session countdown, VWAP -- locally, because those need
    #                  a ticking clock and the chart's own series.
    #
    # All three are best-effort and silent when the EA is away: nothing here
    # is on a trading path, and a blank panel must never be able to raise
    # into the reader loop.

    async def _template_for_selected_slot(self) -> Optional[dict]:
        """The saved template assigned to the panel's selected CH tab, or
        None when that tab has no channel or its channel has no template."""
        from backend.src.services.broker import ea_templates as _et
        from backend.src.services.positions import core_panel_context as _pc
        from backend.src.db import database as db_module
        try:
            reader = getattr(self._engine, "_tg_reader", None)
            name = await db_module.to_db_thread(
                _pc.template_for_slot, reader, self._panel_slot)
            if not name:
                return None
            return await db_module.to_db_thread(_et.get_ea_template, name)
        except Exception as e:
            log.debug("[EABridge] slot template lookup failed: %s", e)
            return None

    async def push_panel_context(self) -> bool:
        from backend.src.services.positions import core_panel_context as _pc
        from backend.src.db import database as db_module
        if not self.is_ea_healthy():
            return False
        try:
            reader = getattr(self._engine, "_tg_reader", None)
            msg = await db_module.to_db_thread(
                _pc.build_context, reader, self._panel_slot)
        except Exception as e:
            log.debug("[EABridge] panel_context build failed: %s", e)
            return False
        return await self._send(msg)

    async def push_panel_signal(self) -> bool:
        from backend.src.services.positions import core_panel_signal as _ps
        if not self.is_ea_healthy():
            return False
        bridge = getattr(self._engine, "_bridge", None)
        if bridge is None:
            return False
        payload = await _ps.build_payload(bridge)
        msg: dict = {"type": "panel_signal"}
        for k, v in payload.items():
            if k == "levels":
                continue
            msg[k] = (1 if v else 0) if isinstance(v, bool) else v
        # Levels are flattened rather than nested: the EA's JSON reader is a
        # flat key scanner by design (see ForexTraderBridge.mq5's JsonGet*),
        # and giving it an array to parse would mean a real parser for one
        # optional display tab.
        for i, lv in enumerate(payload.get("levels") or [], start=1):
            msg[f"lvl{i}_price"] = lv.get("price")
            msg[f"lvl{i}_kind"]  = lv.get("kind")
            msg[f"lvl{i}_dir"]   = lv.get("dir")
        msg["level_count"] = len(payload.get("levels") or [])
        return await self._send(msg)

    async def push_panel_log(self, text: str) -> bool:
        """Append one line to the panel's RECENT SYSTEM LOGS strip.

        Fire-and-forget commentary for things that happen app-side and would
        otherwise be invisible at the terminal (a signal rejected, a template
        pushed). The EA keeps its own ring buffer and interleaves its own
        lines with these.
        """
        if not self.is_ea_healthy():
            return False
        return await self._send({"type": "panel_log", "text": str(text)[:120]})

    async def _panel_loop(self) -> None:
        """Keep the panel's read-only halves current while an EA is connected.

        Cadence is deliberately unequal. The signal payload is candle-derived
        and the mt5 bridge caches candles anyway, so 3s costs almost nothing
        and keeps the criteria row honest. The channel roster only changes
        when someone edits config, so 30s is generous.
        """
        ctx_every = 10          # every 10th signal push -> ~30s
        n = 0
        try:
            while True:
                await asyncio.sleep(3.0)
                if not self.is_ea_healthy():
                    continue
                try:
                    await self.push_panel_signal()
                    if n % ctx_every == 0:
                        await self.push_panel_context()
                except Exception as e:
                    log.debug("[EABridge] panel push failed: %s", e)
                n += 1
        except asyncio.CancelledError:
            raise

    def _ensure_panel_loop(self) -> None:
        if self._panel_task is None or self._panel_task.done():
            self._panel_task = asyncio.create_task(self._panel_loop())

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

        Three kinds of action arrive here:

          * a template FIELD name (anchors, lot_anchor, sl_pips, tp3_pips,
            trail_mode, ...) -- the generic path below, which works for any
            key in core_ea_templates.DEFAULTS with no code here per field.
          * select_channel -- moves the CH tab, which only changes WHICH
            template the panel edits.
          * an order action -- routed to the same app-side functions the UI
            and the Telegram bot use, so a trade started from the chart is
            risk-checked, tracked and logged identically to every other
            trade. The EA does the pip arithmetic (it owns _Point) and sends
            finished prices; this never re-derives them.
        """
        # Local imports: this module is pulled in early by the engine, and
        # importing database/templates at module scope reintroduces the
        # circular import the rest of this file already avoids the same way.
        from backend.src.services.broker import ea_templates as _et
        from backend.src.db import database as db_module
        action = (msg.get("action") or "").strip()
        value  = (msg.get("value") or "").strip()
        name   = (msg.get("template") or "").strip()

        if action == "select_channel":
            try:
                self._panel_slot = max(0, int(float(value)))
            except (TypeError, ValueError):
                return
            tpl = await self._template_for_selected_slot()
            if tpl:
                await self.push_template(tpl["name"], tpl)
            await self.push_panel_context()
            return

        if action in _ORDER_ACTIONS:
            await self._on_panel_order(action, msg, name)
            return

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

    async def _on_panel_order(self, action: str, msg: dict, template: str) -> None:
        """Entry Management buttons: SELL / BUY / SELL LIMIT / BUY LIMIT /
        CANCEL LIMITS / CLOSE ALL.

        Every one of these goes through the app's normal order paths rather
        than being sent by the EA itself. The EA could place them in two
        lines -- it has CTrade right there -- but a trade that appears at the
        broker with no app-side row is invisible to the risk governor, to the
        monitor loop, to reporting, and to the fallback watchdog. The chart
        is a remote control for the app, not a second trader.

        The EA supplies finished prices (price/sl/tp1..tp5/lots), computed
        with its own _Point and the selected template's pip fields. Deriving
        them again here would mean a second pip convention to keep in step --
        see PipsToPrice()'s note in the EA about how easily that goes wrong.

        Outcomes are reported back as panel_log lines, since the terminal has
        no other way to learn that an order it asked for was refused.
        """
        engine = self._engine
        bridge = getattr(engine, "_bridge", None)
        if bridge is None:
            await self.push_panel_log("ORDER REFUSED: no MT5 bridge")
            return
        cfg = getattr(engine, "_cfg", {}) or {}
        starting = float(cfg.get("starting_balance", 1000.0))

        def _f(key: str) -> Optional[float]:
            v = msg.get(key)
            try:
                f = float(v)
            except (TypeError, ValueError):
                return None
            return f if f > 0 else None

        try:
            if action in ("market_buy", "market_sell"):
                from backend.src.services.trading.manual_market_order import (
                    open_manual_market_order)
                direction = "BUY" if action == "market_buy" else "SELL"
                res = await open_manual_market_order(
                    bridge, direction,
                    stop_loss=_f("sl"), lot_size=_f("lots"),
                    take_profit=_f("tp1"),
                    source_name="panel_manual",
                    starting_balance=starting,
                )
                await self.push_panel_log(
                    f"{direction} {res.get('lot_size', '')} @ "
                    f"{float(res.get('entry_price', 0) or 0):.2f}")

            elif action in ("limit_buy", "limit_sell"):
                from backend.src.services.trading.manual_limit_order import (
                    open_manual_limit_order)
                direction = "BUY" if action == "limit_buy" else "SELL"
                price = _f("price")
                sl    = _f("sl")
                if price is None or sl is None:
                    await self.push_panel_log("LIMIT REFUSED: needs price and SL")
                    return
                tps = [_f(f"tp{n}") for n in range(1, 6)]
                if not any(t is not None for t in tps):
                    await self.push_panel_log("LIMIT REFUSED: template has no TP1")
                    return
                res = await open_manual_limit_order(
                    bridge, direction,
                    entry_low=price, entry_high=price, stop_loss=sl,
                    tp1=tps[0], tp2=tps[1], tp3=tps[2], tp4=tps[3], tp5=tps[4],
                    lot_size=_f("lots"),
                    notes=f"chart panel ({template or 'no template'})",
                    starting_balance=starting,
                )
                await self.push_panel_log(
                    f"{direction} LIMIT {res.get('lot_size', '')} @ {price:.2f}")

            elif action == "close_all":
                # The runtime's own close command, not bot_trading.cmd_close:
                # that extraction was deleted (2847e32) while the live copy
                # stayed on the runtime, so the import upstream's panel used
                # raises ImportError here. Found 2026-08-26.
                out = await self._engine.close_cmd(["all"])
                await self.push_panel_log(out.splitlines()[-1] if out else "CLOSE ALL done")

            elif action == "cancel_limits":
                n = await self._cancel_all_working_pendings()
                await self.push_panel_log(f"CANCELLED {n} PENDING")
        except Exception as e:
            log.warning("[EABridge] panel order %s failed: %s", action, e)
            await self.push_panel_log(f"{action.upper()} FAILED: {e}")

    async def _cancel_all_working_pendings(self) -> int:
        """Pull every still-working pending order this app knows about.

        Deliberately driven off vantage_pending_orders rather than off
        OrdersTotal() in the terminal: the app's rows are the ones that would
        otherwise be left marked 'working' forever, and cancelling by trade_id
        routes through cancel_pending_order so the row is closed out properly.
        Orders placed outside this app are not ours to delete.
        """
        from backend.src.db import database as db_module

        rows = await db_module.to_db_thread(broker_repo.fetch_working_pending_orders)
        done = 0
        for row in rows:
            try:
                # ea_ticket, not mt5_ticket -- that is the column name on
                # vantage_pending_orders (see database.py's schema).
                if await self.cancel_pending_order(
                        row["trade_id"], int(row.get("ea_ticket") or 0),
                        "panel_cancel_limits"):
                    done += 1
            except Exception as e:
                log.warning("[EABridge] panel cancel %s failed: %s",
                            row.get("trade_id"), e)
        return done
