"""Pending signal (zone-fill) activation watcher -- extracted from
core/engine.py's SimulationEngine._try_activate_pending_signals, as part
of the core/engine.py migration series. See
docs/todo/refactor/core-pending-signal-activation-migration/020-*.md.

Calls open_trade_from_signal (pack 13) -- a real MT5 order-placement call,
unchanged from the original. This module places no order itself; it only
calls whatever `bridge` its caller supplies, via that already-extracted
function.

`retry_after` (per-signal backoff timestamps) and `dpm_candles` are taken
as explicit parameters -- instance state that isn't derivable from the
database, same pattern as `scale_out_last_fail` in the scale-out handler
pack.

`background_open_commentary` (added at core-engine-wiring time, not part
of the original verbatim extraction) is threaded through to the internal
open_trade_from_signal call the same way `open_manual_market_order` and
`open_trade_from_signal` itself already take it -- without this, wiring
SimulationEngine._try_activate_pending_signals straight to this function
would have silently dropped the AI/Telegram open-commentary notification
for every zone-fill activation, since the original bound method called
self.open_trade_from_signal (which always injects the real callback
internally), not this module's own open_trade_from_signal import.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core import core_ea_templates as ea_templates
from forex_trader.core.core_open_trade_from_signal import open_trade_from_signal
from forex_trader.core.core_risk_governor import check_pre_trade_filters, price_in_entry_range
from forex_trader.core.core_trade_reporting import get_open_trades
from forex_trader.core.models import (
    STRATEGY_SCALE_OUT, STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
    STRATEGY_FIXED_RR,
    STRATEGY_SIGNAL_CLIMBER, STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2,
    Tick,
)

log = logging.getLogger(__name__)

_EXPIRY = 120  # 2 minutes — cancel if zone not filled in time
_GDVR_PENDING_EXPIRY_SEC = 4 * 3600  # signals often take >1h to fill the entry zone
_PENDING_ACTIVATION_BACKOFF_S = 20.0
# EA Templates (2026-07-28) -- matches the 60min TTL a resting Limit Runner
# order gets (core_limit_order_signal._DEFAULT_EXPIRE_MINUTES). Until the
# "High Risk" dispatch fix landed the same day, a template-assigned channel's
# Limit-format signals were being diverted to Limit Runner and so never
# reached this expiry path at all; once they did, the 120s default expired
# every one of them before price could pull back into the zone (confirmed
# live: three GOLD DIGGERS INSTITUTIONAL signals expired unfilled in a row).
_TEMPLATE_PENDING_EXPIRY_SEC = 60 * 60


def _channel_parser_format(source_name: str | None) -> str:
    """The configured parser_format for whichever channel `source_name`
    belongs to, or "" if unknown. source_name on a stored signal is the
    decorated form ("Telegram Auto (<channel>)"), and channel_parser_config
    is keyed by the bare channel name, so the wrapper has to come off before
    the lookup -- and the result is resolved through the canonical-channel
    map so a renamed channel still finds its own row."""
    src = (source_name or "").strip()
    if not src:
        return ""
    if src.lower().startswith("telegram auto (") and src.endswith(")"):
        src = src[len("Telegram Auto ("):-1]
    try:
        cfg = db_module.get_channel_parser_config(db_module._canonical(src))
        return (cfg or {}).get("parser_format", "") or ""
    except Exception:
        return ""


async def try_activate_pending_signals(
    tick: Tick, rs: dict, bridge: Any, retry_after: dict, dpm_candles: list,
    starting_balance: float = 1000.0,
    background_open_commentary: Optional[Callable[[str, dict, Tick], Awaitable[None]]] = None,
) -> bool:
    """Activate queued signals whose entry zone has been reached.

    Called every monitor-loop cycle. Signals that have not
    filled within 2 minutes are expired — the market context will have
    changed and the zone level is stale for a scalping strategy.

    Exception: Reversal Runner's edge depends on zone signals that often
    take well over an hour to fill (median ~101min in the backtest this
    strategy is derived from) — signals get _GDVR_PENDING_EXPIRY_SEC (4h)
    instead of the default whenever Reversal Runner applies to that signal,
    either as the global Active Strategy or as a per-channel override on
    the signal's own source channel (Channel Strategy tab) — a signal
    from a channel overridden to reversal_runner must get the long window
    even while some other channel is driving the global strategy, or
    every GD VIP zone signal expires in 2 minutes before the ~101min
    median fill time, silently starving that strategy of any fills.

    Returns whether any signal was pending at the start of this cycle —
    _monitor_loop uses this to stay on the fast (1s) poll cadence while a
    zone-fill is being awaited, instead of dropping to the idle 5s poll
    just because no trade happens to be open yet (see _monitor_loop).
    """
    now = time.time()
    with db_module.db() as conn:
        pending = [
            db_module.row_to_dict(r)
            for r in conn.execute(
                # Excludes any signal with a currently-resting genuine EA
                # pending order (Limit Runner / ORB auto-execute) -- without
                # this, the moment price re-enters the same entry zone that
                # order is watching, this generic market-fill watcher would
                # race it and open a SECOND, duplicate trade before the real
                # pending order has even filled. Once that order resolves
                # (filled or cancelled), vantage_pending_orders.status stops
                # being 'working' and/or vantage_signals.status itself moves
                # off 'pending' (see ea_bridge._on_pending_order_filled/
                # _on_pending_order_cancelled), so this exclusion is only
                # ever load-bearing during that narrow placed-but-not-yet-
                # resolved window.
                "SELECT * FROM vantage_signals WHERE status='pending' "
                "AND signal_id NOT IN ("
                "  SELECT signal_id FROM vantage_pending_orders WHERE status='working'"
                ") ORDER BY created_at ASC"
            ).fetchall()
        ]
    if not pending:
        return False

    open_trades = get_open_trades()
    open_count  = len(open_trades)
    max_trades  = int(rs.get("max_open_trades", 1))
    current_strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT)

    for sig in pending:
        # Expire signals that did not fill within the allowed window
        channel_strategy = db_module.get_channel_strategy_override(sig.get("source_name"))
        effective_strategy = channel_strategy or current_strategy
        # GD2 signals are published after the provider enters — price typically needs
        # time to pull back to the zone.  Give them 15 min instead of 2 min so brief
        # retracements are not missed.  Reversal Runner, Adaptive Runner, and Adaptive
        # Runner 2 keep the 4h window — any of them can end up on the same
        # slow-to-fill zone signals if a channel is overridden to it, and the wider
        # window is harmless for faster-filling signals (they still fire the moment
        # price re-enters the zone; this only raises how long they're allowed to wait).
        _src = (sig.get("source_name") or "").lower()
        # Was `"gold diggers 2.0" in _src` -- a hardcoded PRE-RENAME channel
        # name. That group's Telegram title changed to "GOLD DIGGERS
        # INSTITUTIONAL" (same group_id), so the test silently became dead
        # code and every one of its zone signals dropped to the 120s default
        # instead of the 15 minutes this branch exists to give them. Same
        # class of bug as the orphaned channel_performance row fixed the same
        # day. Resolved through the channel's configured parser_format
        # instead of its display name, so no future rename can break it again
        # -- "gd2" IS the format these pullback-style zone signals arrive in,
        # which is what the window was actually about.
        _is_gd2_src = _channel_parser_format(sig.get("source_name")) == "gd2"
        _is_orb_src = "orb/ivb report" in _src
        if ea_templates.is_template_override(effective_strategy):
            _expiry = _TEMPLATE_PENDING_EXPIRY_SEC
        elif effective_strategy in (STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
                                   STRATEGY_ADAPTIVE_RUNNER_2):
            _expiry = _GDVR_PENDING_EXPIRY_SEC
        elif _is_gd2_src:
            _expiry = 15 * 60  # 15 minutes — GD2 limit orders often need a pullback
        elif _is_orb_src:
            # The "reload zone" reference (POC-to-VAH/VAL of the opening
            # hour) stays meaningful for a while after the breakout — a
            # genuine pullback-and-retest can take a while to show up —
            # but unlike GD VIP's 4h window, this zone is tied to a single
            # morning's opening range specifically, not something worth
            # still trading hours later. 60 minutes covers a normal
            # retest without holding a stale zone open all day.
            _expiry = 60 * 60
        else:
            _expiry = _EXPIRY
        age = now - float(sig.get("created_at") or now)
        if age > _expiry:
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_signals SET status='expired' WHERE signal_id=?",
                    (sig["signal_id"],),
                )
            log.info("[PendingWatcher] Signal %s expired — no zone fill after %.0fs",
                     sig["signal_id"][:8], age)
            if _is_orb_src:
                # Otherwise a morning where price never retraced into the
                # reload zone looks identical to a silent failure — no
                # trade, no explanation. The report/email already went
                # out describing the plan, so the absence of a follow-up
                # trade needs its own explicit "and here's why" signal.
                asyncio.create_task(telegram_alerts.send_message(
                    f"*ORB/IVB Reload Zone Not Retested*\n"
                    f"{sig['direction']} zone ${float(sig['entry_low']):.2f}-"
                    f"${float(sig['entry_high']):.2f} — price never came back after "
                    f"{age/60:.0f} min. No trade taken today.",
                    None, "orb_zone_expired",
                ))
            retry_after.pop(sig["signal_id"], None)
            continue

        # Back off after a failed activation attempt instead of retrying
        # the identical order every monitor cycle.
        if now < retry_after.get(sig["signal_id"], 0):
            continue

        # Wait until price re-enters the zone
        if not price_in_entry_range(
            sig["direction"], float(sig["entry_low"]), float(sig["entry_high"]), tick
        ):
            continue

        # Respect the max-trades cap
        if open_count >= max_trades:
            break

        # Pre-trade filters: R:R and directional cap.
        # Resolve channel override here (same 3-tier logic as open_trade_from_signal)
        # so the R:R skip matches the strategy that will actually be used.
        _pw_src = sig.get("source_name") or ""
        _pw_ov  = db_module.get_channel_strategy_override(_pw_src)
        if _pw_ov == "auto":
            _pw_rec = db_module.get_channel_strategy_rec(_pw_src)
            _pw_strategy = _pw_rec.get("strategy") or current_strategy
        elif _pw_ov:
            _pw_strategy = _pw_ov
        else:
            _pw_strategy = current_strategy
        _self_mgd = {STRATEGY_CONSERVATIVE, STRATEGY_CONSERVATIVE_TRIAL,
                     STRATEGY_SIGNAL_CLIMBER, STRATEGY_FIXED_RR,
                     STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
                     STRATEGY_ADAPTIVE_RUNNER_2}
        _act_px = tick.ask if sig["direction"].upper() == "BUY" else tick.bid
        filter_err = None if _pw_strategy in _self_mgd else check_pre_trade_filters(
            sig["direction"], float(sig["entry_low"]), float(sig["entry_high"]),
            float(sig["stop_loss"]), sig.get("tp1"),
            actual_price=_act_px,
            source_name=_pw_src,
        )
        if filter_err:
            log.debug("[PendingWatcher] Signal %s skipped — %s",
                      sig["signal_id"][:8], filter_err)
            continue

        # Guard: if _execute_live already opened a trade for this signal,
        # don't open a second one — just mark it activated and skip.
        with db_module.db() as conn:
            _existing = conn.execute(
                "SELECT trade_id FROM vantage_simulated_trades "
                "WHERE signal_id=? AND status IN ('open','pending')",
                (sig["signal_id"],),
            ).fetchone()
        if _existing:
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_signals SET status='activated' WHERE signal_id=?",
                    (sig["signal_id"],),
                )
            log.info(
                "[PendingWatcher] Signal %s already has an open trade (%s) — skipping duplicate activation",
                sig["signal_id"][:8], _existing[0][:8],
            )
            continue

        # Momentum confirmation: last completed M5 candle must align with direction.
        # A contrary candle suggests the move into the zone is a fakeout/reversal.
        _direction_up = sig["direction"].upper()
        if dpm_candles:
            _lc = dpm_candles[-1]
            _lc_open  = float(_lc.get("open",  0) or 0)
            _lc_close = float(_lc.get("close", 0) or 0)
            if _lc_open and _lc_close:
                _lc_bull = _lc_close > _lc_open
                if (_direction_up == "BUY" and not _lc_bull) or \
                   (_direction_up == "SELL" and _lc_bull):
                    log.info(
                        "[PendingWatcher] Signal %s deferred — last M5 candle is %s "
                        "but trade direction is %s (momentum mismatch)",
                        sig["signal_id"][:8],
                        "bearish" if not _lc_bull else "bullish",
                        _direction_up,
                    )
                    continue

        # All fills within the 2-minute window are treated as fresh (full lot)
        _age_lot_mult = 1.0

        # Price is back in zone — activate
        log.info("[PendingWatcher] Signal %s %s zone $%.2f–$%.2f — activating (age %.0fs)",
                 sig["signal_id"][:8], sig["direction"],
                 float(sig["entry_low"]), float(sig["entry_high"]), age)
        try:
            trade_result = await open_trade_from_signal(
                bridge, sig["signal_id"], age_lot_mult=_age_lot_mult,
                dpm_candles=dpm_candles, starting_balance=starting_balance,
                background_open_commentary=background_open_commentary,
            )
            open_count += 1
            # Flip the TG signal row from 'pending' → 'activated' so the UI updates
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_tg_signals SET status='activated'"
                    " WHERE signal_id=? AND status='pending'",
                    (sig["signal_id"],),
                )
            asyncio.create_task(telegram_alerts.send_message(
                (
                    f"*Zone fill activated*\n"
                    f"{sig['direction']} queued signal entered zone "
                    f"${float(sig['entry_low']):.2f}–${float(sig['entry_high']):.2f}\n"
                    f"Opened @ ${trade_result.get('entry_price', '?'):.2f}"
                ),
                sig["signal_id"], "pending_zone_fill",
            ))
            retry_after.pop(sig["signal_id"], None)
        except Exception as exc:
            _exc_msg = str(exc)
            retry_after[sig["signal_id"]] = now + _PENDING_ACTIVATION_BACKOFF_S
            if "R:R filter" in _exc_msg or "circuit breaker" in _exc_msg.lower():
                log.debug("[PendingWatcher] Signal %s not activated (expected): %s",
                          sig["signal_id"][:8], exc)
            elif "stood down" in _exc_msg.lower():
                # Expected: this node is not the active trader — the VPS handles it.
                log.debug("[PendingWatcher] Signal %s deferred to active node (stood down)",
                          sig["signal_id"][:8])
            else:
                log.warning("[PendingWatcher] Could not activate signal %s: %s — "
                            "backing off %.0fs before retrying",
                            sig["signal_id"][:8], exc, _PENDING_ACTIVATION_BACKOFF_S)

    return True
