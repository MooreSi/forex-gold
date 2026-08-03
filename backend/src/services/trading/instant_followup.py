"""IME follow-up application flow -- extracted verbatim (no logic changes)
from core/engine.py's SimulationEngine._apply_followup_to_instant_trade/
_find_and_apply_instant_followup/_ime_timeout_watchdog, as part of the
core/engine.py migration series. See
docs/todo/refactor/core-instant-followup-migration/020-*.md.

Calls bridge.modify_order -- a real MT5 order-modify call, unchanged from
the original. This module modifies no order itself; it only calls whatever
`bridge` its caller supplies.

Reuses core_update_signal.update_signal (already extracted) for the
signal-linked path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from backend.src.db import database as db_module
from backend.src.services.trading import trade_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.trading.update_signal import update_signal
from backend.src.utils.models import (
    STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL, STRATEGY_BE_RUNNER, Tick,
)
from backend.src.services.risk import expert_params


def ime_timeout_secs() -> int:
    """How long an instant trade waits for its SL/TP follow-up before the
    watchdog assigns them. Was a 3-minute constant; now Settings > Expert
    Tunables."""
    return expert_params.get("ime_followup_timeout_s")


log = logging.getLogger(__name__)

_IME_SELF_MANAGED = {STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL}


async def apply_followup_to_instant_trade(
    instant_trade: dict,
    parsed: dict,
    tg_id: str,
    channel_name: str,
    source_label: str,
    bridge: Any,
) -> None:
    """Apply SL/TP from a full follow-up signal to an open instant market trade."""
    trade_id  = instant_trade["trade_id"]
    signal_id = instant_trade.get("signal_id")

    # Strategies that calculate their own SL/TP from the actual fill price must
    # not have those levels overwritten by the follow-up signal's levels.
    # Conservative / Conservative Trial set exact 5-pt levels post-fill —
    # acknowledge the follow-up (so the IME watchdog stops waiting) but do
    # not apply SL/TP changes.
    if instant_trade.get("strategy") in _IME_SELF_MANAGED:
        # Guard: the IME path previously ignored channel strategy overrides and
        # opened trades with the global default (e.g. conservative) even when the
        # channel explicitly uses a different strategy (e.g. reversal_runner).
        # Detect this mismatch now: if the channel override points to a different
        # strategy, correct the trade's strategy record and fall through to apply
        # the actual SL/TP from the full signal.
        _ch_ov_fu = db_module.get_channel_strategy_override(channel_name)
        _trade_strategy = instant_trade.get("strategy")
        _mismatch = (
            _ch_ov_fu
            and _ch_ov_fu != "auto"
            and _ch_ov_fu != _trade_strategy
        )
        if _mismatch:
            log.info(
                "[IME] Follow-up: strategy mismatch — trade %s opened as %s "
                "but channel %s sets %s — correcting and applying signal levels",
                trade_id[:8], _trade_strategy, channel_name, _ch_ov_fu,
            )
            trade_repo.set_trade_strategy(trade_id, _ch_ov_fu)
            # Rebuild instant_trade with the corrected strategy so the
            # TP validity check and MT5 modify below use it correctly.
            instant_trade = dict(instant_trade)
            instant_trade["strategy"] = _ch_ov_fu
            # Fall through — do not return — so SL/TP from the full signal are applied.
        else:
            log.info(
                "[IME] Follow-up acknowledged but not applied to trade %s "
                "(strategy=%s self-manages SL/TP from fill price)",
                trade_id[:8], _trade_strategy,
            )
            if signal_id:
                trade_repo.set_tg_followup_applied(tg_id, signal_id)
            return

    updates: dict = {
        "stop_loss":  parsed["stop_loss"],
        "entry_low":  parsed["entry_low"],
        "entry_high": parsed["entry_high"],
    }
    for i in range(1, 9):
        v = parsed.get(f"tp{i}")
        if v is not None:
            updates[f"tp{i}"] = v

    # ── TP validity check ────────────────────────────────────────────────
    # If the follow-up zone is shifted relative to our actual fill price,
    # most (or all) of the signal's TPs may land behind us — making the
    # conservative partial-close strategy useless.  Count how many TPs
    # are genuinely reachable from the actual entry fill.
    _ime_dir   = instant_trade.get("direction", "").upper()
    _ime_entry = float(instant_trade.get("entry_price") or 0)
    _ime_sign  = 1.0 if _ime_dir == "BUY" else -1.0
    _valid_tp_count = sum(
        1 for i in range(1, 9)
        if updates.get(f"tp{i}") is not None and (
            (_ime_dir == "BUY"  and float(updates[f"tp{i}"]) > _ime_entry) or
            (_ime_dir == "SELL" and float(updates[f"tp{i}"]) < _ime_entry)
        )
    )
    if _valid_tp_count < 2:
        _old_tp1 = updates.get("tp1")
        _TP_AUTO_OFFSETS = [3.0, 5.0, 7.0, 10.0, 14.0, 18.0]
        for _i, _offset in enumerate(_TP_AUTO_OFFSETS, start=1):
            updates[f"tp{_i}"] = round(_ime_entry + _ime_sign * _offset, 2)
        updates["tp7"] = None
        updates["tp8"] = None
        log.info(
            "[IME] Follow-up TPs adjusted — only %d valid TP(s) from signal "
            "(entry=%.2f, dir=%s) — using auto-spacing from actual entry",
            _valid_tp_count, _ime_entry, _ime_dir,
        )
        _old_tp1_str = f"${float(_old_tp1):.2f}" if _old_tp1 is not None else "none"
        asyncio.create_task(telegram_alerts.send_message(
            f"*⚠️ IME Follow-up — TPs Adjusted*\n"
            f"Signal TP1 was {_old_tp1_str} but entry was ${_ime_entry:.2f} "
            f"({_ime_dir}) — only {_valid_tp_count} valid target(s) reachable.\n"
            f"Auto-assigned TPs from entry:\n"
            f"TP1 ${updates['tp1']:.2f}  TP2 ${updates['tp2']:.2f}  "
            f"TP3 ${updates['tp3']:.2f}  TP4 ${updates['tp4']:.2f}  "
            f"TP5 ${updates['tp5']:.2f}  TP6 ${updates['tp6']:.2f}",
            event_type="ime_tp_adjusted",
        ))
    # ────────────────────────────────────────────────────────────────────

    # ── SL distance sanity check ─────────────────────────────────────────
    # The follow-up's stop_loss is an absolute price the channel computed
    # relative to ITS OWN suggested entry zone (entry_low/entry_high) --
    # but IME exists specifically to fill BEFORE that zone is confirmed
    # (on a bare "BUY NOW"), so the actual fill can sit well outside it.
    # Applying the follow-up SL verbatim in that case can put the stop far
    # further from our real fill than the channel ever intended -- update_
    # signal() only checks it's on the correct SIDE of entry, not how far
    # away it is. Confirmed live on ticket 1641075009: signal zone
    # 4098-4102 (SL 4096, ~4pt intended risk from zone mid) but the IME
    # fill was 4116.24 -- applying 4096 verbatim gave a 20.24pt stop,
    # well past the original 15pt/$150 provisional cap and the strategy's
    # own (adaptive_runner) widening math. Re-derive the SAME point-distance
    # the channel intended and apply it from the actual fill instead,
    # mirroring the TP reachability fix above.
    _signal_zone_mid  = (float(parsed["entry_low"]) + float(parsed["entry_high"])) / 2
    _intended_sl_dist = abs(_signal_zone_mid - float(parsed["stop_loss"]))
    _actual_sl_dist   = abs(_ime_entry - float(parsed["stop_loss"]))
    if _intended_sl_dist > 0 and _actual_sl_dist > _intended_sl_dist * 1.5:
        _old_sl = updates["stop_loss"]
        updates["stop_loss"] = round(_ime_entry - _ime_sign * _intended_sl_dist, 2)
        log.info(
            "[IME] Follow-up SL adjusted — signal SL %.2f implied %.1fpt from actual "
            "entry %.2f (channel intended ~%.1fpt from its own zone) — using %.2f instead",
            _old_sl, _actual_sl_dist, _ime_entry, _intended_sl_dist, updates["stop_loss"],
        )
        asyncio.create_task(telegram_alerts.send_message(
            f"*⚠️ IME Follow-up — SL Adjusted*\n"
            f"Signal SL was ${_old_sl:.2f} ({_actual_sl_dist:.1f}pt from actual entry "
            f"${_ime_entry:.2f}) — channel's own zone implied ~{_intended_sl_dist:.1f}pt risk.\n"
            f"Using ${updates['stop_loss']:.2f} instead.",
            event_type="ime_sl_adjusted",
        ))
    # ────────────────────────────────────────────────────────────────────

    if signal_id:
        await update_signal(bridge, signal_id, updates)
    else:
        # Fallback — no linked signal record; update the trade row directly
        trade_repo.update_trade_fields(trade_id, updates)
        mt5_ticket = instant_trade.get("mt5_ticket")
        if mt5_ticket:
            try:
                _dir    = instant_trade.get("direction", "").upper()
                _entry  = float(instant_trade.get("entry_price") or 0)
                _strat  = instant_trade.get("strategy", "")
                safe_tp = None
                if _strat == STRATEGY_BE_RUNNER:
                    for _tk in ("tp8", "tp7", "tp6", "tp5", "tp4", "tp3", "tp2", "tp1"):
                        _raw = parsed.get(_tk)
                        if _raw is not None:
                            _tp_f = float(_raw)
                            if (_dir == "BUY" and _tp_f > _entry) or \
                               (_dir == "SELL" and _tp_f < _entry):
                                safe_tp = _raw
                            break
                await bridge.modify_order(
                    int(mt5_ticket), sl=parsed["stop_loss"], tp=safe_tp
                )
            except Exception as exc:
                log.warning("[IME] Follow-up modify_order failed: %s", exc)

            # Same EA-staleness gap as update_signal() below — this branch
            # has no signal_id to route through that function, so it needs
            # its own push of the corrected TP levels into the EA's
            # in-memory tracking (see update_trade()'s docstring).
            if instant_trade.get("managed_by") == "ea":
                try:
                    from backend.src.services.broker import ea_bridge as _ea_mod
                    _ea = _ea_mod.get_instance()
                    if _ea is not None:
                        _new_tps = {
                            n: float(updates[f"tp{n}"])
                            for n in range(1, 9)
                            if updates.get(f"tp{n}") is not None
                        }
                        if _new_tps:
                            await _ea.update_trade(trade_id, _new_tps)
                except Exception as exc:
                    log.warning("EA update_trade after IME follow-up failed: %s", exc)

    trade_repo.set_tg_followup_applied(tg_id, signal_id)

    sl_px   = float(parsed["stop_loss"])
    tp1_px  = parsed.get("tp1")
    tp1_str = f"TP1: ${float(tp1_px):.2f}" if tp1_px else "no TP1"
    log.info("[IME] Follow-up applied to trade %s: SL=%.2f %s",
             trade_id[:8], sl_px, tp1_str)
    asyncio.create_task(telegram_alerts.send_message(
        telegram_alerts.fmt_instant_followup(instant_trade, parsed, channel_name),
        event_type="instant_followup",
    ))


async def find_and_apply_instant_followup(
    channel_name: str, direction: str, parsed: dict, tg_id: str, bridge: Any,
) -> bool:
    """Locate the open instant-entry trade this follow-up signal belongs
    to and apply it, on whichever node actually holds that trade's row.

    Under centralized signal generation with the VPS as active trader,
    the IME trade this follow-up is meant for was opened via open_trade()'s
    forwarding branch and only exists in the VPS's own vantage_simulated_trades
    — never this (generating) node's. Querying the local table here always
    found nothing, so every follow-up fell through to opening a second,
    independent trade: the VPS ended up placing two real MT5 orders for one
    signal. Mirrors open_trade()'s own forwarding condition exactly so the
    two stay in sync — if a signal was forwarded to open, its follow-up
    must be forwarded to update the same node's copy.

    Returns True if a matching open instant trade was found (locally or on
    the VPS) and the follow-up was applied to it — the caller should skip
    opening a new trade. False means no match — fall through as normal.
    """
    try:
        from backend.src.controllers.sync.protocol import TRADER_REMOTE_VPS
        from backend.src.controllers.sync.client import SyncClient, get_instance as _sync_cli_instance
        _host, _, _ = SyncClient.load_config()
        if _host and db_module.get_active_trader() == TRADER_REMOTE_VPS:
            _rs_fu = await db_module.to_db_thread(db_module.get_risk_settings)
            if _rs_fu.get("centralized_signal_gen_enabled"):
                _cli = _sync_cli_instance()
                _updates = {
                    "stop_loss": parsed["stop_loss"],
                    "entry_low": parsed["entry_low"],
                    "entry_high": parsed["entry_high"],
                }
                for _i in range(1, 9):
                    _updates[f"tp{_i}"] = parsed.get(f"tp{_i}")
                try:
                    _ack = await _cli.send_signal_followup(
                        channel_name, direction, _updates, tg_id,
                    )
                    return bool(_ack.get("matched"))
                except Exception as _fu_exc:
                    # No worse than the pre-fix behaviour (which never
                    # matched at all under forwarding) — log and fall
                    # through to the local query below, which will also
                    # find nothing and correctly return False so the
                    # caller opens a new (normally-forwarded) trade
                    # rather than silently dropping the follow-up.
                    log.warning(
                        "[IME] Follow-up forward to VPS failed (%s) — "
                        "signal tg_id=%s will open as a new trade instead",
                        _fu_exc, tg_id,
                    )
                    return False
    except ImportError:
        pass

    _irow = trade_repo.find_latest_instant_trade(channel_name)
    _instant = db_module.row_to_dict(_irow) if _irow else None
    if not _instant or _instant.get("direction", "").upper() != direction.upper():
        return False
    await apply_followup_to_instant_trade(
        _instant, parsed, tg_id, channel_name, channel_name, bridge,
    )
    return True


async def ime_timeout_watchdog(tick: "Tick", bridge: Any) -> None:
    """
    Auto-assign SL/TP levels to IME trades that have been open for more than
    3 minutes without receiving a follow-up signal.

    When the follow-up never arrives the trade sits with no TP levels and only
    a provisional emergency SL, making conservative strategy inoperative (no
    partial closes, no SL-to-breakeven).  This watchdog:

    1. Finds open trades with tp1=NULL and open_time > 3 minutes ago.
    2. Assigns six standard XAUUSD TP levels (3/5/7/10/14/18 pts from entry).
    3. If the current price has already cleared auto-TP1, moves the SL to
       entry (breakeven) so the trade is at least protected.
    4. Calls modify_order on MT5 so broker-side SL is updated.
    5. Sends a Telegram alert so the operator knows auto-TPs were assigned.
    """
    _IME_TIMEOUT_SEC   = ime_timeout_secs()
    _TP_OFFSETS        = [3.0, 5.0, 7.0, 10.0, 14.0, 18.0]  # pts from entry

    now = time.time()
    rows = trade_repo.fetch_open_trades_missing_tp1(now - _IME_TIMEOUT_SEC)

    if not rows:
        return

    for row in rows:
        trade       = db_module.row_to_dict(row)
        trade_id    = trade["trade_id"]
        direction   = trade["direction"].upper()
        entry_price = float(trade["entry_price"])
        current_sl  = float(trade["stop_loss"])
        mt5_ticket  = trade.get("mt5_ticket")
        age_min     = int((now - float(trade["open_time"])) / 60)

        # EA-managed trades (built-in ladder strategies and EA Templates
        # alike) are protected by the EA's own on-tick logic -- this
        # watchdog must not also guess a generic fallback TP ladder and
        # force SL to breakeven out from under it. Same class of bug as
        # core_tp_safety_net.py's own managed_by=='ea' guard (ticket
        # 1556670216); confirmed live 2026-07-23 on an EA Template with
        # tpsl_mode="off"/"stealth" and no TP by design -- this watchdog
        # force-assigned a 6-level fallback ladder and moved SL to
        # breakeven via a raw MT5 modify_order call, completely bypassing
        # the template's own stealth/harvest/trail management.
        if trade.get("managed_by") == "ea":
            try:
                from backend.src.services.broker import ea_bridge as _ea_mod
                _ea = _ea_mod.get_instance()
                if _ea is not None and _ea.is_ea_healthy():
                    continue
            except Exception:
                pass

        # Self-managing strategies set their own SL/TP immediately after fill.
        # If tp1 is still NULL something went wrong with that block — log a
        # warning but do NOT overwrite with generic auto-assign levels.
        if trade.get("strategy") in _IME_SELF_MANAGED:
            log.warning(
                "[IME-timeout] %s trade %s still has no TP after %dm — "
                "skipping auto-assign (strategy self-manages levels from fill price)",
                trade.get("strategy"), trade_id[:8], age_min,
            )
            continue

        sign = 1.0 if direction == "BUY" else -1.0
        cur_px = tick.ask if direction == "BUY" else tick.bid

        # ── Auto-assign TPs ───────────────────────────────────────────────
        tp_vals = [round(entry_price + sign * off, 2) for off in _TP_OFFSETS]
        tp1_val = tp_vals[0]

        # ── SL: move to breakeven if price has already cleared auto-TP1 ──
        at_be       = False
        new_sl      = current_sl
        sl_moved_db = int(trade.get("sl_moved_to_be") or 0)
        if (direction == "BUY"  and cur_px >= tp1_val) or \
           (direction == "SELL" and cur_px <= tp1_val):
            new_sl      = entry_price
            at_be       = True
            sl_moved_db = 1

        # ── Update DB ──────────────────────────────────────────────────────
        trade_repo.apply_followup_watchdog_levels(
            trade_id, tuple(tp_vals[:6]), new_sl, sl_moved_db)

        # ── Update MT5 ─────────────────────────────────────────────────────
        if mt5_ticket:
            try:
                await bridge.modify_order(
                    int(mt5_ticket), sl=new_sl, tp=None,
                )
            except Exception as exc:
                log.warning("[IME-timeout] modify_order failed ticket=%s: %s",
                            mt5_ticket, exc)

        sl_note = f"moved to entry (breakeven)" if at_be else "provisional (unchanged)"
        log.info(
            "[IME-timeout] Trade %s %s — auto-assigned TPs %.2f/%.2f/%.2f/%.2f/%.2f/%.2f "
            "age=%dm SL=%s",
            trade_id[:8], direction,
            *tp_vals[:6], age_min, sl_note,
        )
        asyncio.create_task(telegram_alerts.send_message(
            f"*⏱ IME Auto-TPs Assigned* ({direction})\n"
            f"No follow-up after {age_min} min — auto-assigned targets:\n"
            f"TP1 ${tp_vals[0]:.2f}  TP2 ${tp_vals[1]:.2f}  "
            f"TP3 ${tp_vals[2]:.2f}  TP4 ${tp_vals[3]:.2f}\n"
            f"TP5 ${tp_vals[4]:.2f} _(exit)_  TP6 ${tp_vals[5]:.2f} _(headroom)_\n"
            f"SL: ${new_sl:.2f} — {sl_note}",
            event_type="ime_timeout",
        ))
