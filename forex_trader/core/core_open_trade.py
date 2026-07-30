"""Trade opening -- extracted verbatim (no logic changes) from
core/engine.py's SimulationEngine.open_trade, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-open-trade-migration/020-*.md.

Places an order via bridge.place_order (Python bridge path) or the EA
bridge's open_trade (EA-managed path) -- the real MT5 order-placement
calls, unchanged from the original. This module places no order itself;
it only calls whatever `bridge` (and whatever ea_bridge/sync singletons
are configured) its caller supplies.

Takes `bridge` as an explicit parameter instead of self._bridge/
self.get_fresh_tick(). ea_bridge, sync.server, sync.client, db_module, and
core_risk_governor.is_trading_paused (pack 1) are imported and used
directly -- all already accessed via module-level get_instance()
singletons or db_module in the original, so no new state-carrier class is
needed here (unlike pack 10's CloseTradeContext).
"""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Optional

from forex_trader.core import database as db_module
from forex_trader.core import core_ea_templates as ea_templates
from forex_trader.core.core_risk_governor import is_trading_paused, price_in_entry_range
from forex_trader.core.models import (
    Tick, STRATEGY_SCALE_OUT, STRATEGY_BE_RUNNER,
    STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_FIXED_RR,
    STRATEGY_ADAPTIVE_RUNNER_2,
)

log = logging.getLogger(__name__)

# ── EA-ladder lookup tables (verbatim copies -- also used elsewhere in
# engine.py by the signal_climber/reversal_runner handlers, out of scope here) ──

_CLIMBER_PCTS: dict[int, list[float]] = {
    1: [1.00],
    2: [0.40, 0.60],
    3: [0.30, 0.30, 0.40],
    4: [0.20, 0.25, 0.25, 0.30],
    5: [0.20, 0.15, 0.15, 0.20, 0.30],
    6: [0.20, 0.15, 0.15, 0.15, 0.20, 0.15],
    7: [0.20, 0.10, 0.10, 0.15, 0.15, 0.20, 0.10],
    8: [0.20, 0.10, 0.10, 0.10, 0.15, 0.15, 0.10, 0.10],
}

_GDVR_PCTS: dict[int, list[float]] = {
    1: [1.00],
    2: [0.30, 0.70],
    3: [0.15, 0.25, 0.60],
    4: [0.10, 0.15, 0.25, 0.50],
    5: [0.10, 0.10, 0.15, 0.25, 0.40],
    6: [0.08, 0.08, 0.12, 0.17, 0.20, 0.35],
    7: [0.07, 0.07, 0.10, 0.13, 0.13, 0.20, 0.30],
    8: [0.05, 0.05, 0.10, 0.10, 0.15, 0.15, 0.15, 0.25],
}

# EA handoff: the three _run_tp_ladder-shaped strategies send their close-%
# table and BE-trigger position to the EA at trade-open time (see
# ea_bridge.EABridge.open_trade's pcts/be_at_pos params) rather than the EA
# hardcoding its own copy — Python stays the single source of truth, so a
# tuning change here takes effect on EA-managed trades with no MQL5 rebuild.
_EA_LADDER_PCTS = {
    STRATEGY_SIGNAL_CLIMBER: _CLIMBER_PCTS,
    STRATEGY_REVERSAL_RUNNER: _GDVR_PCTS,
    STRATEGY_ADAPTIVE_RUNNER: _GDVR_PCTS,
    # Adaptive Runner 2's own stated 8-TP schedule (5/5/10/10/15/15/15/25) is
    # an exact match for _GDVR_PCTS -- reuses the same table rather than
    # duplicating it, same as Adaptive Runner does.
    STRATEGY_ADAPTIVE_RUNNER_2: _GDVR_PCTS,
}
_EA_LADDER_BE_AT_POS = {
    STRATEGY_SIGNAL_CLIMBER: 0,
    STRATEGY_REVERSAL_RUNNER: 1,
    STRATEGY_ADAPTIVE_RUNNER: 0,
    STRATEGY_ADAPTIVE_RUNNER_2: 1,   # BE at TP2, not TP1
}
# EA-side SL-trail rule -- see ManageLadder() in ForexTraderBridge.mq5.
# "prev_tp" (the default the EA already had, sent when this key is absent)
# trails to the single immediately-previous TP price. Adaptive Runner 2 is
# the first strategy to need a different rule -- see run_tp_ladder()'s
# sl_rule parameter (core_run_tp_ladder.py) for the Python-side equivalent,
# which this table must stay in lockstep with.
_EA_LADDER_TRAIL_MODE = {
    STRATEGY_ADAPTIVE_RUNNER_2: "midpoint_lag2",
}


def is_telegram_source(tg_source: Optional[str]) -> bool:
    """Did this signal come from a Telegram message, as opposed to one of the
    app's own internal generators or a manual order?

    Decided against the registry of configured Telegram channels
    (channel_parser_config, the same table the Channel Strategy tab builds
    its list from) rather than against a hardcoded list of engine names, so
    adding a channel or renaming one needs no change here. The
    'Telegram Auto (<name>)' spelling the auto-execution path writes is
    unwrapped first, since that is the form most executed rows carry.
    """
    if not tg_source:
        return False
    name = tg_source.strip()
    if name.startswith("Telegram Auto (") and name.endswith(")"):
        name = name[len("Telegram Auto ("):-1]
    try:
        canon = db_module._canonical(name)
    except Exception:
        canon = name
    try:
        configured = {c.get("channel_name") for c in db_module.get_all_channel_parser_configs()}
    except Exception:
        return False
    return canon in configured or name in configured


def resolve_template_tps(template: dict, direction: str, tick: Any,
                          signal_tps: list, tg_source: Optional[str]) -> tuple:
    """Work out the TP levels an EA Template trade actually opens with.

    Returns (tps, pcts, pending_pips):
      tps          {n: absolute price} -- the anchor ladder, and what goes
                   on the wire as tp1..tp8.
      pcts         close-% list aligned to sorted(tps), or None to leave the
                   EA on its own defaults.
      pending_pips None  -> send the template's own tp_pen{n}_pips unchanged
                   {}    -> zero them, so the EA falls back to the absolute
                            tp{n} values (see HandleOpenTemplateGrid)
                   {n: pips} -> replace them with these

    Resolution order per ladder: the message's own TP prices when
    "Use TP Levels from Telegram" is set AND this is a Telegram signal AND
    that message actually stated levels; otherwise the template's pips
    column. Internal generators never take the Telegram branch -- they have
    no message -- which is exactly why the pips columns stay editable while
    the flag is on.
    """
    sign  = 1 if direction.upper() == "BUY" else -1
    ref   = tick.ask if direction.upper() == "BUY" else tick.bid
    # The EA stages a grid from the OPPOSITE side of the book than Python's
    # own reference price (basePrice = bid for a BUY, ask for a SELL, see
    # HandleOpenTemplateGrid) -- so a pips value handed to the EA must be
    # measured from that side, or every pending level lands one spread out.
    ea_base = tick.bid if direction.upper() == "BUY" else tick.ask

    msg_tps = [float(t) for t in signal_tps if t]
    from_tg = bool(msg_tps) and is_telegram_source(tg_source)
    use_tg_anchor  = bool(template.get("tp_from_telegram")) and from_tg
    use_tg_pending = bool(template.get("tp_pen_from_telegram")) and from_tg

    def _pips_ladder(prefix: str) -> dict:
        out = {}
        for n in range(1, ea_templates.MAX_TP_LEVELS + 1):
            pips = float(template.get(f"{prefix}{n}_pips", 0.0) or 0.0)
            if pips > 0:
                out[n] = ref + sign * pips
        return out

    if use_tg_anchor:
        # The message sets the level COUNT: 3 stated TPs is a 3-level ladder,
        # not 3 levels padded out to whatever the pct column happens to
        # define. Levels are taken in the message's own order.
        tps = {n: price for n, price in
               enumerate(msg_tps[:ea_templates.MAX_TP_LEVELS], start=1)}
    else:
        tps = _pips_ladder("tp")

    pcts = None
    if tps:
        table = [float(template.get(f"tp{n}_pct", 0.0) or 0.0) for n in sorted(tps)]
        if use_tg_anchor and table:
            # Whatever ends up being the last level closes the remainder, so
            # a message with more TPs than the pct column defines can never
            # leave the position with an unclosable tail. Only applied on
            # the Telegram branch -- a template-driven ladder deliberately
            # allows a trailing 0% (Sig Gen Grid runs TP5/TP6 at 0 and lets
            # the trailing stop take them).
            table[-1] = 100.0
        if any(p > 0 for p in table):
            pcts = table

    pending_pips = None
    if use_tg_pending:
        if use_tg_anchor:
            # Anchor and pending both on the message: the absolute tp{n}
            # already carry exactly the levels wanted, and the EA uses them
            # verbatim for a pending leg whose pips entry is 0.
            pending_pips = {}
        else:
            # Anchor stayed on the template while pending follows the
            # message. The absolute tp{n} on the wire are the anchor's
            # template levels, so the fallback would hand the pending legs
            # the wrong ladder -- convert the message's prices into pips
            # from the EA's own staging base instead, which reproduces them
            # under the default "unified" anchor mode.
            pending_pips = {
                n: abs(price - ea_base)
                for n, price in enumerate(msg_tps[:ea_templates.MAX_TP_LEVELS], start=1)
            }
    return tps, pcts, pending_pips


async def open_trade(
    bridge: Any,
    signal_id: str, direction: str, entry_low: float, entry_high: float,
    stop_loss: float,
    tp1=None, tp2=None, tp3=None, tp4=None, tp5=None,
    tp6=None, tp7=None, tp8=None,
    lot_size: float = 0.01, tick: Optional[Tick] = None,
    strategy: str = STRATEGY_SCALE_OUT,
    tg_source: Optional[str] = None,
    mt5_tp_override: Optional[float] = None,
) -> dict:
    # Remote stand-down — the Local/Remote sync feature's mutual-exclusion
    # gate. Deliberately separate from the manual pause flag below: pause
    # can be cleared by the user's Resume button at any time, which must
    # NEVER be able to re-arm this node while the other node believes it
    # still holds control. Only RESUME over the sync channel clears this.
    # No-op (near-zero cost) on any install that hasn't configured sync —
    # get_instance() returns None until sync.server.init() has run.
    try:
        from forex_trader.sync import server as _sync_srv_mod
        _srv = _sync_srv_mod.get_instance()
        if _srv is not None and _srv.is_standing_down():
            raise ValueError(
                "Trading stood down — the paired Local/Remote node has taken "
                "control (see Settings > Remote Node)."
            )
    except ImportError:
        pass

    # Mirror of the check above, for the client (Mac) role — the VPS-side
    # is_standing_down() check only ever fires on whichever node runs the
    # sync SERVER (the VPS), so without this the Mac's own open_trade()
    # was never actually gated by the Local/Remote toggle at all: setting
    # the Mac to "Remote" mode stopped its Breakout/Bounce/Reversal Engine sub-
    # engines but left its own Telegram-signal execution running
    # unconditionally, causing the same incoming signal (both nodes run
    # separate Telethon sessions on the same Telegram account) to open
    # duplicate real trades on the shared MT5 account from both sides at
    # once. Only applies if this Mac has actually been paired with a VPS
    # (a standalone install with no pairing configured is never gated).
    try:
        from forex_trader.sync.protocol import TRADER_REMOTE_VPS
        from forex_trader.sync.client import SyncClient
        _host, _, _ = SyncClient.load_config()
        if _host and db_module.get_active_trader() == TRADER_REMOTE_VPS:
            # Centralized signal generation (Settings > Remote Node): this
            # node keeps analyzing (should_generate_signals_here() only
            # ever returns False on the VPS side), but the VPS has
            # stopped analyzing entirely under this mode, so forward the
            # already-fully-resolved trade there for execution instead of
            # just failing. Mirrors the manual Market Order button's
            # MSG_MARKET_ORDER forwarding (sync/client.py:send_market_order)
            # but carries this call's full parameter set, since the VPS
            # calls open_trade() directly rather than re-resolving
            # anything via open_trade_from_signal.
            _rs = await db_module.to_db_thread(db_module.get_risk_settings)
            if _rs.get("centralized_signal_gen_enabled"):
                from forex_trader.sync.client import get_instance as _sync_cli_instance
                _cli = _sync_cli_instance()
                _ack = await _cli.send_signal_order(
                    signal_id=signal_id, direction=direction,
                    entry_low=entry_low, entry_high=entry_high, stop_loss=stop_loss,
                    tp1=tp1, tp2=tp2, tp3=tp3, tp4=tp4, tp5=tp5,
                    tp6=tp6, tp7=tp7, tp8=tp8,
                    lot_size=lot_size, strategy=strategy, tg_source=tg_source,
                )
                # _ack is the raw {"type": "signal_order_ack", "result": {...}}
                # or {"type": ..., "error": "..."} message — callers (
                # open_trade_from_signal, open_manual_market_order) expect
                # this call to return the same shape as a local open_trade()
                # (a plain {"trade_id", "mt5_ticket", ...} dict) or raise on
                # failure, exactly like a local execution would. Returning
                # the ack envelope unwrapped previously left result["trade_id"]
                # missing (KeyError further down every forwarded call).
                if _ack.get("error"):
                    raise RuntimeError(f"VPS rejected forwarded trade: {_ack['error']}")
                _result = dict(_ack.get("result") or {})
                _result["executed_remotely"] = True
                return _result
            raise ValueError(
                "Trading stood down — the VPS is the active trader "
                "(see Settings > Remote Node)."
            )
    except ImportError:
        pass

    # Trading pause — blocks MT5 order placement only; signals and generators continue.
    if await db_module.to_db_thread(is_trading_paused):
        _pause_until = db_module.get_app_config("trade_pause_until")
        try:
            _until_str = time.strftime("%H:%M", time.localtime(float(_pause_until)))
            _pause_msg = f"Trading paused until {_until_str} — MT5 order blocked."
        except Exception:
            _pause_msg = "Trading paused — MT5 order blocked."
        log.warning("[Pause] %s (signal=%s)", _pause_msg, signal_id[:8])
        raise ValueError(_pause_msg)

    # Global circuit breaker — blocks all live MT5 execution.
    _cb = db_module.get_circuit_breaker_state()
    if _cb["is_active"]:
        _cb_mins = int(_cb["remaining_secs"] // 60) + 1
        raise ValueError(
            f"Trade blocked — circuit breaker active. "
            f"Live trading resumes in approximately {_cb_mins} min."
        )

    # Max-open-trades gate — lives here (the function that actually inserts
    # the trade row) rather than in the callers, so it's checked against
    # whichever node's table is about to receive the INSERT.
    rs = await db_module.to_db_thread(db_module.get_risk_settings)
    with db_module.db() as conn:
        open_count = conn.execute(
            "SELECT COUNT(*) FROM vantage_simulated_trades WHERE status='open'"
        ).fetchone()[0]
    if open_count >= int(rs.get("max_open_trades", 1)):
        raise ValueError(f"Max open trades reached ({rs.get('max_open_trades', 1)})")

    # Use caller-supplied tick if available — callers on the critical path
    # (open_trade_from_signal, _process_instant_entry) already hold a fresh
    # tick so fetching again wastes one bridge round-trip.
    if tick is None:
        tick = await bridge.get_fresh_tick()
    if tick is None:
        raise RuntimeError("No live price available")

    if mt5_tp_override is not None:
        # Adaptive Runner ladder legs (see open_adaptive_runner_ladder):
        # each leg is its own MT5 position with its own genuine resting
        # broker-side TP, set at open time — not managed by Python
        # tick-polling like every other strategy's ladder.
        mt5_tp = mt5_tp_override
    elif strategy == STRATEGY_BE_RUNNER:
        mt5_tp = tp8 or tp7 or tp6 or tp5 or tp4 or tp3 or tp2 or tp1
    elif strategy == STRATEGY_FIXED_RR:
        # Fixed R:R's whole design is one broker stop and one broker
        # target -- nothing polls it, so MT5 still closes the trade
        # correctly if the app or the EA is down. tp1 carries the
        # computed target (see core_open_trade_from_signal, which
        # rewrites both levels to be exact from the real fill).
        mt5_tp = tp1
    else:
        mt5_tp = None

    # Hand off to the companion MQL5 EA when it's enabled, connected, and
    # this strategy's SL/TP/partial-close rules are ones it can run
    # natively on OnTick — see forex_trader.core.ea_bridge for why DPM
    # never qualifies. Falls straight through to the existing bridge
    # path (unchanged) if the EA is off, unreachable, or the strategy
    # isn't portable, so this is purely additive — no existing behaviour
    # changes when the EA isn't in the picture.
    trade_id = str(uuid.uuid4())[:16]
    now      = time.time()
    managed_by = "python"
    mt5_ticket = None
    entry_price = None
    # The TP levels an EA Template resolved to, so the row below can record
    # what the trade is ACTUALLY running instead of the signal's own levels.
    # Stays None for every non-template strategy, which keeps writing the
    # signal levels exactly as before.
    _resolved_tps: Optional[dict] = None
    _is_template = ea_templates.is_template_override(strategy)
    ea_rs = await db_module.to_db_thread(db_module.get_risk_settings)
    if bool(ea_rs.get("ea_bridge_enabled", 0)) and mt5_tp_override is None:
        try:
            from forex_trader.core import ea_bridge as _ea_mod
            _ea = _ea_mod.get_instance()
            # TEMP diagnostic — every adaptive_runner trade today has
            # gone Python-only despite ea_bridge_enabled=1, no [EA] log
            # line at all, cause not yet identified (2026-07-17).
            log.info(
                "[EA-diag] handoff check strategy=%s ea_instance=%s ea_healthy=%s portable=%s",
                strategy, _ea is not None,
                _ea.is_ea_healthy() if _ea is not None else None,
                _ea.is_strategy_portable(strategy) if _ea is not None else None,
            )
            if _ea is not None and _ea.is_ea_healthy() and _ea.is_strategy_portable(strategy):
                _tps = {n: v for n, v in enumerate(
                    [tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8], start=1
                ) if v is not None}
                _ea_pcts = None
                _ea_be_at_pos = None
                _ea_trail_mode = None
                _ea_template = None
                if strategy in _EA_LADDER_PCTS and _tps:
                    _ea_table = _EA_LADDER_PCTS[strategy]
                    _ea_pcts = _ea_table.get(len(_tps), _ea_table[max(_ea_table)])
                    _ea_be_at_pos = _EA_LADDER_BE_AT_POS[strategy]
                    _ea_trail_mode = _EA_LADDER_TRAIL_MODE.get(strategy)
                elif _is_template:
                    _ea_template = ea_templates.get_ea_template(
                        ea_templates.template_name_from_override(strategy)
                    )
                    if _ea_template is not None:
                        # Anchor TP (2026-07-29): the template's tp{n}_pips
                        # is now AUTHORITATIVE for every level -- entry ±
                        # N pips from the actual fill reference price,
                        # replacing the signal's own TP prices entirely
                        # rather than only filling gaps it left. Explicit
                        # user directive: an EA Template channel's targets
                        # come from the template, full stop, so the same
                        # channel behaves identically regardless of which
                        # provider's message shape triggered the trade.
                        # tp{n}_pct is unchanged -- it was always template-
                        # only, since a signal states TP prices but never
                        # how much to close at each one.
                        #
                        # A template with no pips configured at any level
                        # sends NO take-profit at all (an empty _tps),
                        # rather than silently reviving the discarded
                        # signal levels as an implicit fallback -- that
                        # would defeat the point of this being explicit and
                        # authoritative. tpsl_mode/mode still govern
                        # whether/how this ladder is used by the EA.
                        #
                        # "Use TP Levels from Telegram" (2026-07-30) can
                        # override the pips column per ladder -- see
                        # resolve_template_tps, which owns the whole
                        # resolution and is what the DB row is written from
                        # below so the record matches what the EA runs.
                        _tps, _ea_pcts, _pen_pips = resolve_template_tps(
                            _ea_template, direction, tick,
                            [tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8], tg_source,
                        )
                        _resolved_tps = dict(_tps)
                        if _pen_pips is not None:
                            # Rewrite the pending ladder on the COPY sent to
                            # the EA only -- the stored template keeps the
                            # user's own pips for when the flag is off or the
                            # signal came from an internal generator.
                            _ea_template = dict(_ea_template)
                            for n in range(1, ea_templates.MAX_TP_LEVELS + 1):
                                _ea_template[f"tp_pen{n}_pips"] = float(_pen_pips.get(n, 0.0))
                _ea_lot = lot_size
                if _ea_template is not None and _ea_template.get("mode") == "grid":
                    # Global Parameters > Fixed Lot Size (Grid) -- used for
                    # EACH leg the EA stages in HandleOpenTemplateGrid, in
                    # place of whatever lot_size normal (non-grid) sizing
                    # computed above. 0 (default) leaves lot_size untouched.
                    _grid_lot = float(ea_rs.get("strategy_lot_size_grid", 0))
                    if _grid_lot > 0:
                        _ea_lot = _grid_lot

                    # ── Anchor legs require price to BE at the zone ──────
                    # A grid's anchor is a MARKET order (HandleOpenTemplateGrid
                    # calls trade.Buy/trade.Sell for it). That is only an entry
                    # the signal asked for while price is at or better than its
                    # zone -- which used to be guaranteed, because a grid was
                    # only ever dispatched once price had reached the zone.
                    # Placing on arrival removed that guarantee, and on
                    # 2026-07-30 six queued Reversal Engine signals with zones
                    # from 4084 to 4121 each took a market anchor at ~4095:
                    # four BUYs and two SELLs filled within seconds of each
                    # other, none at a price its own signal named.
                    #
                    # Outside the zone, only the resting legs are placed --
                    # those sit AT the zone by construction and are the whole
                    # reason for staging early. The anchor's own purpose
                    # ("take part of the position now so a signal that never
                    # retraces is not missed entirely") is exactly what must
                    # not happen when price is already past the level.
                    if not price_in_entry_range(direction, entry_low, entry_high, tick):
                        _pendings = int(_ea_template.get("pendings", 0) or 0) or \
                                    int(_ea_template.get("grid_legs", 0) or 0)
                        if _pendings <= 0:
                            # Nothing left to place: anchors-only template with
                            # price away from the zone. Refuse rather than fall
                            # through to a market fill -- the signal stays
                            # pending and is retried while its window lasts.
                            raise RuntimeError(
                                f"Grid template '{_ea_template.get('name', '?')}' has no pending "
                                f"legs and price is outside the {direction} zone "
                                f"{entry_low:.2f}-{entry_high:.2f} — refusing a market anchor "
                                f"at a price this signal never named"
                            )
                        _px = tick.ask if direction.upper() == "BUY" else tick.bid
                        log.info(
                            "[EA] grid staged without anchors: price %.2f is outside the %s "
                            "zone %.2f-%.2f — %d resting leg(s) only",
                            _px, direction, entry_low, entry_high, _pendings,
                        )
                        _ea_template = dict(_ea_template)
                        _ea_template["anchors"] = 0
                # A template's ack is only sent once the EA has staged EVERY
                # leg (HandleOpenTemplateGrid's closing SendJson), and each
                # leg is its own synchronous broker round trip -- so the flat
                # 5s default is a function of leg count, not a constant. It
                # was exceeded live on 2026-07-30 with 1 anchor + 3 pendings,
                # which is what set off the runaway described below.
                _ack_timeout = 5.0
                if _ea_template is not None:
                    _legs = (int(_ea_template.get("anchors", 0) or 0)
                             + int(_ea_template.get("pendings", 0) or 0))
                    _ack_timeout = min(60.0, 10.0 + 5.0 * max(1, _legs))
                try:
                    ea_ack = await _ea.open_trade(
                        trade_id, direction.upper(), _ea_lot, stop_loss, _tps, strategy,
                        pcts=_ea_pcts, be_at_pos=_ea_be_at_pos, trail_mode=_ea_trail_mode,
                        template=_ea_template,
                        # The signal's own entry zone -- grid-mode templates stage
                        # their legs across it instead of stepping away from
                        # current price. See EABridge.open_trade's zone_low/
                        # zone_high handling for why.
                        zone_low=entry_low, zone_high=entry_high,
                        timeout=_ack_timeout,
                    )
                except asyncio.TimeoutError:
                    if not _is_template:
                        raise
                    # A timed-out ack does NOT mean nothing happened -- the EA
                    # may already have real legs on the book, and on
                    # 2026-07-30 it did. Raising here used to abort before the
                    # INSERT, so no row existed, the signal stayed 'pending',
                    # and PendingWatcher re-activated it every 20s: 5 signals
                    # became ~133 opens and 36 untracked live positions, none
                    # of which the app could see or close.
                    #
                    # Record the same placeholder row the EA's own grid ack
                    # produces (ticket 0 / entry 0) instead. Whatever the EA
                    # actually placed is then reconciled by the existing
                    # paths -- a leg fill promotes the row, and
                    # core_template_placeholder_repair adopts or closes it
                    # from the broker's own records if that event never
                    # arrives. Critically the signal also flips to 'active'
                    # below, which is what stops the re-activation loop.
                    log.error(
                        "[EA] template open ack timed out after %.0fs (trade=%s, "
                        "strategy=%s) -- the EA may have placed legs; recording a "
                        "placeholder for reconciliation rather than retrying",
                        _ack_timeout, trade_id[:8], strategy,
                    )
                    ea_ack = {"type": "trade_opened", "ticket": 0, "fill_price": 0.0,
                              "ack_timed_out": True}
                if ea_ack.get("type") == "trade_opened":
                    mt5_ticket  = ea_ack.get("ticket")
                    entry_price = float(ea_ack.get("fill_price", 0))
                    managed_by  = "ea"
                    log.info("[EA] order placed: ticket=%s dir=%s lots=%s @ %s (strategy=%s)",
                             mt5_ticket, direction, _ea_lot, entry_price, strategy)
                elif _is_template:
                    raise RuntimeError(f"EA rejected template order: {ea_ack.get('error')}")
                else:
                    log.warning("[EA] open_trade rejected: %s — falling back to Python bridge",
                                ea_ack.get("error"))
            elif _is_template:
                raise RuntimeError(
                    "EA Template strategies require a connected, healthy EA — "
                    "none is available right now"
                )
        except Exception as _ea_e:
            if _is_template:
                raise
            log.warning("[EA] handoff failed (%s) — falling back to Python bridge", _ea_e)

    if _is_template and managed_by != "ea":
        # Should be unreachable (every path above either sets managed_by='ea'
        # or raises for a template strategy) -- fail loudly rather than let a
        # template trade silently fall through to the Python bridge path,
        # which has no idea how to run Grid/Stealth/Anchor/Trail management.
        raise RuntimeError("EA Template strategy resolved with no EA management — refusing to open")

    if managed_by != "ea":
        mt5_result  = await bridge.place_order(direction, lot_size, stop_loss, mt5_tp,
                                               f"sig:{signal_id[:8]}")
        mt5_error   = mt5_result.get("error")

        # Raise immediately — do NOT record a phantom trade if MT5 rejected the order
        if mt5_error:
            log.error("MT5 order failed: %s", mt5_error)
            raise RuntimeError(f"MT5 order rejected: {mt5_error}")

        mt5_ticket  = mt5_result.get("ticket")
        entry_price = float(mt5_result.get("fill_price") or
                            (tick.ask if direction.upper() == "BUY" else tick.bid))
        log.info("MT5 order placed: ticket=%s dir=%s lots=%s @ %s",
                 mt5_ticket, direction, lot_size, entry_price)

    # Record the levels the trade is actually running. For an EA Template
    # these are the template's (or, with "Use TP Levels from Telegram" on,
    # the message's) resolved prices -- NOT the signal's own tp1..tp8, which
    # is what this row used to store while the EA ran something else
    # entirely. That divergence is what made the trade-open alert, the UI and
    # TP Safety Net all report levels no one was trading (found 2026-07-30:
    # a Sig Gen Grid trade alerted 8 Reversal Engine levels while the EA ran
    # the template's 6). A level the resolved ladder doesn't define is NULL,
    # so a 6-level template stops claiming 8.
    if _resolved_tps is not None:
        _row_tps = [_resolved_tps.get(n) for n in range(1, 9)]
    else:
        _row_tps = [tp1, tp2, tp3, tp4, tp5, tp6, tp7, tp8]

    with db_module.db() as conn:
        conn.execute(
            """INSERT INTO vantage_simulated_trades
               (trade_id,signal_id,mt5_ticket,direction,entry_low,entry_high,entry_price,
                lot_size,remaining_lots,stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,
                status,open_time,spread_cost,commission,slippage_cost,net_pnl,strategy,tg_source,
                managed_by)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (trade_id, signal_id, mt5_ticket, direction.upper(), entry_low, entry_high, entry_price,
             lot_size, lot_size, stop_loss, *_row_tps,
             "open", now,
             0.0, 0.0, 0.0, 0.0,
             strategy, tg_source, managed_by),
        )
        conn.execute(
            "UPDATE vantage_signals SET status='active' WHERE signal_id=?", (signal_id,)
        )

    return {
        "trade_id":    trade_id,
        "mt5_ticket":  mt5_ticket,
        "entry_price": entry_price,
        "strategy":    strategy,
        "managed_by":  managed_by,
    }
