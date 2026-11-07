"""Run a virtual Reversal trade on the EA template it is stamped with.

docs/todo/030. Every signal carries the strategy a real order would run under
(`_active_strategy`: schedule window, else the channel's pick, else the global
one). Until 2026-11-07 a `template:<name>` stamp was ignored here and the
signal was managed on the 50/30/20 REF ladder, so its outcome -- the ML
gate's training label -- described a trade no real order made.

This walks the stamped template tick by tick through `TickWalk`, the same
rules the Backtest tab's tick replay uses: the template's stop from the fill,
its TP ladder and partials, breakeven and trail. The walk's state and the
template as it stood at the fill are saved on the row after every tick, so a
restart resumes the same trade and a template edited mid-trade does not
change one already running.

A template the walk cannot model, or one that no longer exists, keeps the
ladder, and the row says why (`virtual_exit_json.reason`). Decided once, at
the first tick, and kept for the life of the trade.

Fills follow the ladder's conventions so the two stay comparable: partials
at the closing side of the tick, a stop at its price within the spread plus
`_STOP_SLIP_MAX_PTS` (`_stop_fill`), the same fee model, the same 0.1 lot.

Virtual only. Executed signals never reach this (`_check_outcomes` routes
them to `_reconcile_live_signal`), and nothing here talks to a broker.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from backend.src.services.backtest import template_simulator as _ts
from backend.src.services.backtest.template_tick_walk import TickWalk
from backend.src.services.reversal_engine import reversal_engine_repo as re_db
from backend.src.services.reversal_engine.reversal_engine_manage import _VIRTUAL_LOT

_log = logging.getLogger("reversal_engine")

_PREFIX = "template:"


def _load_template(name: str) -> Optional[dict]:
    from backend.src.services.broker import ea_templates
    return ea_templates.get_ea_template(name)


def _why_not(template: Optional[dict], name: str) -> str:
    """Why the walk cannot run this template, or ""."""
    if template is None:
        return f"template '{name}' no longer exists"
    why = _ts.unsupported_reason(template, tick_mode=True)
    if why:
        return why
    if _ts.pips_to_price(float(template.get("sl_pips") or 0.0)) <= 0:
        return ("template states no stop (sl_pips 0): a real order then keeps "
                "the signal's own stop, which this walk does not model")
    return ""


def _plan(sig: dict) -> dict:
    """The saved plan for this signal, made and saved at its first tick."""
    saved = sig.get("virtual_exit_json")
    if saved:
        return json.loads(saved)
    name = str(sig.get("strategy") or "")[len(_PREFIX):]
    template = _load_template(name)
    why = _why_not(template, name)
    if why:
        plan = {"model": "ladder", "template": name, "reason": why}
        re_db.save_virtual_exit(sig["id"], json.dumps(plan), None)
        _log.info("[RE-Engine] %s keeps the ladder: %s", sig.get("signal_ref", sig["id"]), why)
        return plan
    entry = float(sig.get("trigger_price")
                  or (float(sig["entry_low"]) + float(sig["entry_high"])) / 2)
    walk = TickWalk(template, entry, sig["direction"] == "BUY", _VIRTUAL_LOT)
    plan = {"model": "template", "template": name, "risk": walk.risk,
            "definition": template, "state": walk.to_state()}
    re_db.save_virtual_exit(sig["id"], json.dumps(plan, default=str), walk.risk)
    re_db.set_stop_loss(sig["id"], walk.stop)
    return plan


async def manage(eng, sig: dict, tick) -> bool:
    """Manage `sig` on its template. False when the ladder should instead."""
    try:
        plan = _plan(sig)
    except Exception as exc:
        _log.warning("[RE-Engine] %s template plan failed, ladder used: %s",
                     sig.get("signal_ref", sig.get("id")), exc)
        return False
    if plan.get("model") != "template":
        return False
    try:
        _step(eng, sig, tick, plan)
    except Exception as exc:
        # Not handed to the ladder: mixing two managements on one trade
        # would make its label describe neither.
        _log.warning("[RE-Engine] %s template step failed: %s",
                     sig.get("signal_ref", sig.get("id")), exc)
    return True


def _step(eng, sig: dict, tick, plan: dict) -> None:
    sig_id, direction = sig["id"], sig["direction"]
    walk = TickWalk.from_state(plan["definition"], plan["state"])
    entry = walk.entry
    stop_before = walk.stop
    price = eng._realistic_fill(tick, direction, closing=True)
    try:
        fav = (price - entry) if direction == "BUY" else (entry - price)
        re_db.record_excursion(sig_id, fav, -fav)
    except Exception:
        pass

    events = walk.step(float(tick.bid), float(tick.ask))
    booked = float(sig.get("partial_pnl_dollars") or 0.0)
    for n, ev in enumerate(events):
        frac = ev.lots / _VIRTUAL_LOT
        fill = eng._stop_fill(tick, direction, ev.price) if ev.kind == "sl" else ev.price
        leg_pts = (fill - entry) if direction == "BUY" else (entry - fill)
        gross, net = eng._net_pnl(sig, leg_pts, tick)
        if walk.done and n == len(events) - 1:
            _close(eng, sig, fill, leg_pts, booked, gross * frac, net * frac)
        else:
            leg_net = round(net * frac, 2)
            re_db.book_partial_close(sig_id, leg_net, frac, tp_idx=ev.tp_number or 0)
            booked += leg_net
            _log.info("[RE-Engine] TP%s hit %s (template %s) -> banked $%.2f (%.0f%%)",
                      ev.tp_number, sig.get("signal_ref", sig_id), plan["template"],
                      leg_net, frac * 100)

    plan["state"] = walk.to_state()
    re_db.save_virtual_exit(sig_id, json.dumps(plan, default=str), plan.get("risk"))
    if walk.stop is not None and walk.stop != stop_before and not walk.done:
        re_db.set_stop_loss(sig_id, walk.stop)
    if events or walk.stop != stop_before:
        eng._notify_refresh()


def _close(eng, sig: dict, fill: float, leg_pts: float, booked: float,
           gross_leg: float, net_leg: float) -> None:
    """Close what remains. Outcome by the trade's total, as the ladder does."""
    sig_id, direction = sig["id"], sig["direction"]
    total_net = round(booked + net_leg, 2)
    outcome = "win" if total_net > 0.5 else "loss" if total_net < -0.5 else "be"
    re_db.close_signal(
        sig_id, fill, outcome, round(leg_pts, 2),
        net_pnl_dollars=total_net,
        pnl_dollars=round(booked + gross_leg, 2),
        balance_delta=round(net_leg, 2),
    )
    from backend.src.services.reversal_engine import ml_engine as re_ml
    re_ml.record_outcome(sig_id, outcome)
    try:
        from backend.src.db import database as _cdb
        _cdb.close_bus_entry("reversal_engine", sig_id)
    except Exception:
        pass
    try:
        from backend.src.services.cluster.sync.ledger import push_trade_closed
        push_trade_closed({
            "trade_id":    sig.get("signal_ref") or str(sig_id),
            "engine":      "reversal_engine",
            "direction":   direction,
            "strategy":    sig.get("strategy", ""),
            "open_time":   sig.get("created_at"),
            "close_time":  time.time(),
            "pnl_dollars": total_net,
            "outcome":     outcome,
            "tg_source":   "Reversal Engine",
            "mt5_ticket":  sig.get("mt5_ticket"),
        })
    except Exception as exc:
        _log.debug("[Ledger] push failed: %s", exc)
    _log.info("[RE-Engine] CLOSED %s (template) outcome=%s total_net=$%.2f",
              sig.get("signal_ref", sig_id), outcome, total_net)
