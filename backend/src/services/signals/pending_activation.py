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

from backend.src.db import database as db_module
from backend.src.services.signals import repo as signals_repo
from backend.src.services.signals import tg_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.broker import ea_templates as ea_templates
from backend.src.services.positions.core_grid_template_dispatch import grid_template
from backend.src.services.trading.open_from_signal import open_trade_from_signal
from backend.src.services.risk.governor import check_pre_trade_filters, price_in_entry_range
from backend.src.services.analytics.reporting import get_open_trades
from backend.src.utils.models import (
    STRATEGY_SCALE_OUT,
    STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
    STRATEGY_ADAPTIVE_RUNNER_2,
    Tick,
)
# One source of truth for "this strategy's own levels replace the signal's,
# so scoring the signal's R:R would decline trades on numbers it never uses"
# -- shared with the fresh-signal scan path so the two cannot drift apart.
from backend.src.services.trading.scan_auto_execute import (
    _PRE_TRADE_FILTER_BYPASS_STRATEGIES,
)
from backend.src.services.risk import expert_params

log = logging.getLogger(__name__)

_EXPIRY = 120  # 2 minutes -- the shipped default; the live value is expiry_secs()


def expiry_secs() -> int:
    """The base expiry for a queued signal: cancel it if its zone is not
    refilled in time.

    Was the bare `_EXPIRY` constant; it is Settings > Expert Tunables
    (`pending_signal_expiry_s`) since the tunables work, and stays a function
    so a change takes effect without a restart. `_EXPIRY` remains as the
    documented shipped default and the fallback if the tunable is unreadable,
    which is also what upstream's tests compare against.

    Only the DEFAULT branch of _resolve_expiry_sec uses this -- every longer
    window there exists for its own documented reason and is not governed by
    it."""
    try:
        return int(expert_params.get("pending_signal_expiry_s"))
    except Exception:
        return _EXPIRY

# Immediate Market Entry OFF (2026-08-06, explicit user directive). With IME
# on, a signal is taken at market the moment it lands and never reaches this
# queue at all. With it off, the signal's whole premise is that price comes
# back to the zone before entering -- so the wait is the feature, and 2
# minutes is short enough that a normal retracement misses it. 3 minutes is
# the directive's own figure. Deliberately applied only where the 120s
# default would otherwise apply: every longer window below (template, the
# runner strategies, gd2, ORB) exists for a documented reason and must not
# be shortened to 3 minutes by this. Limit-format signals are excluded --
# their resting broker order IS the wait, on its own 60min TTL (see
# core_limit_order_signal._DEFAULT_EXPIRE_MINUTES).
_IME_OFF_EXPIRY = 180
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

# How many failed activation attempts a single signal gets before it is given
# up on rather than retried until it expires.
#
# There was no cap at all before 2026-07-30. With a 20s backoff and a 1h
# template expiry that allows ~180 attempts on ONE signal, and an attempt is
# not free: a grid template stages real broker legs before the failure is
# even detectable, so a repeatable failure mid-open compounds into live
# positions. It did -- 5 signals produced ~133 activations and 36 untracked
# positions when the EA's open ack began timing out. The specific timeout is
# fixed in core_open_trade, but the cap is what makes ANY future failure mode
# cost a bounded number of orders instead of an unbounded one.
_MAX_ACTIVATION_ATTEMPTS = 3

# signal_id -> consecutive failed activation attempts. Module state rather
# than a new parameter, so every existing caller of this function keeps its
# signature; pruned on success, expiry and give-up.
_ACTIVATION_FAILURES: dict[str, int] = {}


def _bare_channel(source_name: str | None) -> str:
    """The bare channel name behind a stored signal's decorated source_name
    ("Telegram Auto (<channel>)"). channel_parser_config is keyed by the
    bare name, so the wrapper has to come off before any lookup."""
    src = (source_name or "").strip()
    if src.lower().startswith("telegram auto (") and src.endswith(")"):
        src = src[len("Telegram Auto ("):-1]
    return src


def _channel_parser_format(source_name: str | None) -> str:
    """The configured parser_format for whichever channel `source_name`
    belongs to, or "" if unknown. The result is resolved through the
    canonical-channel map so a renamed channel still finds its own row."""
    src = _bare_channel(source_name)
    if not src:
        return ""
    try:
        cfg = db_module.get_channel_parser_config(db_module._canonical(src))
        return (cfg or {}).get("parser_format", "") or ""
    except Exception:
        return ""


def _ime_enabled_for_source(rs: dict, source_name: str | None) -> bool:
    """Whether Immediate Market Entry is live for the channel behind a
    stored signal's decorated source_name. Same gate the Telegram scan path
    applies (see core_scan_messages_auto_execute.ime_enabled_for_channel):
    the global toggle AND the per-channel flag, whose default depends on
    parser format."""
    src = _bare_channel(source_name)
    if not src:
        return False
    try:
        from backend.src.services.trading.scan_auto_execute import (
            ime_enabled_for_channel,
        )
        return ime_enabled_for_channel(rs, db_module._canonical(src))
    except Exception:
        return False


def _is_limit_signal(signal_id: str | None) -> bool:
    """True when this signal was placed as a genuine broker-side pending
    order (Limit Runner / "[LIMITS] ... AREA" format). Tested by the
    existence of its vantage_pending_orders row rather than its status, so
    a placed order that has since been cancelled or expired still counts --
    the point is what KIND of signal it is, not where it got to."""
    if not signal_id:
        return False
    try:
        return signals_repo.has_pending_order(signal_id)
    except Exception:
        return False


def _resolve_effective_strategy(source_name: str | None, current_strategy: str) -> str:
    """The strategy a queued signal from `source_name` would actually run
    under: channel override > Auto's current recommendation > the global
    Active Strategy.

    Extracted 2026-08-27 because this function had two different answers to
    that question -- an `override or global` one driving the expiry ladder
    and the grid dispatch, and a three-tier one driving the R:R bypass. They
    disagreed for exactly the case that matters most: a channel on Auto,
    which is the default.
    """
    src = source_name or ""
    override = db_module.get_channel_strategy_override(src)
    if override == "auto":
        rec = db_module.get_channel_strategy_rec(src)
        return (rec.get("strategy") or "").strip() or current_strategy
    return override or current_strategy


def _resolve_expiry_sec(sig: dict, rs: dict, effective_strategy: str) -> float:
    """How long this queued signal is allowed to wait for its zone.

    Extracted from try_activate_pending_signals' loop (2026-08-06) so the
    ladder is directly testable -- it was previously reachable only by
    driving the whole activation coroutine. Behaviour is unchanged apart
    from the _IME_OFF_EXPIRY branch added at the same time.

    GD2 signals are published after the provider enters, so price typically
    needs time to pull back to the zone: 15 min instead of 2. Reversal
    Runner, Adaptive Runner and Adaptive Runner 2 keep the 4h window -- any
    of them can end up on the same slow-to-fill zone signals if a channel is
    overridden to it, and the wider window is harmless for faster-filling
    signals (they still fire the moment price re-enters the zone; this only
    raises how long they're allowed to wait).
    """
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
        return _TEMPLATE_PENDING_EXPIRY_SEC
    if effective_strategy in (STRATEGY_REVERSAL_RUNNER, STRATEGY_ADAPTIVE_RUNNER,
                              STRATEGY_ADAPTIVE_RUNNER_2):
        return _GDVR_PENDING_EXPIRY_SEC
    if _is_gd2_src:
        return 15 * 60  # 15 minutes — GD2 limit orders often need a pullback
    if _is_orb_src:
        # The "reload zone" reference (POC-to-VAH/VAL of the opening
        # hour) stays meaningful for a while after the breakout — a
        # genuine pullback-and-retest can take a while to show up —
        # but unlike GD VIP's 4h window, this zone is tied to a single
        # morning's opening range specifically, not something worth
        # still trading hours later. 60 minutes covers a normal
        # retest without holding a stale zone open all day.
        return 60 * 60
    # See _IME_OFF_EXPIRY. Last branch before the default on purpose, so it
    # only ever widens the 120s case and never shortens one of the longer
    # windows above.
    if (not _is_limit_signal(sig.get("signal_id"))
            and not _ime_enabled_for_source(rs, sig.get("source_name"))):
        return _IME_OFF_EXPIRY
    return expiry_secs()


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
    pending = signals_repo.get_pending_signals_awaiting_zone_fill()
    if not pending:
        return False

    open_trades = get_open_trades()
    open_count  = len(open_trades)
    max_trades  = int(rs.get("max_open_trades", 1))
    current_strategy = rs.get("trade_strategy", STRATEGY_SCALE_OUT)

    for sig in pending:
        # Expire signals that did not fill within the allowed window
        # Resolve the strategy this signal would actually run under, once,
        # and use the same answer everywhere below. This used to be
        # `override or global` here and a separate three-tier resolution
        # further down, so a channel left on Auto -- the default -- had the
        # literal string "auto" scored against the expiry ladder and the grid
        # dispatch: never a runner strategy, never a template, so it fell to
        # the shortest default window regardless of what Auto had actually
        # picked for it.
        effective_strategy = _resolve_effective_strategy(
            sig.get("source_name"), current_strategy)
        # GD2 signals are published after the provider enters — price typically needs
        # time to pull back to the zone.  Give them 15 min instead of 2 min so brief
        # retracements are not missed.  Reversal Runner, Adaptive Runner, and Adaptive
        # Runner 2 keep the 4h window — any of them can end up on the same
        # slow-to-fill zone signals if a channel is overridden to it, and the wider
        # window is harmless for faster-filling signals (they still fire the moment
        # price re-enters the zone; this only raises how long they're allowed to wait).
        # Grid templates place on arrival rather than on zone re-entry -- the
        # EA's own resting legs are the wait (see core_grid_template_dispatch).
        # A signal that reached this queue at all (manual add, sync push, bot
        # /addsignal, ORB report) never went through the Telegram fast path's
        # equivalent branch, so without this it sat here until price came back
        # and then opened at market -- exactly what a grid exists to avoid.
        _grid_tpl = grid_template(effective_strategy)
        # Also drives the ORB-specific "zone never filled" alert below, which
        # is why it stays here rather than living only inside the expiry
        # ladder that _resolve_expiry_sec now owns.
        _is_orb_src = "orb/ivb report" in (sig.get("source_name") or "").lower()
        _expiry = _resolve_expiry_sec(sig, rs, effective_strategy)
        age = now - float(sig.get("created_at") or now)
        if age > _expiry:
            signals_repo.expire_signal(sig["signal_id"])
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
            _ACTIVATION_FAILURES.pop(sig["signal_id"], None)
            continue

        # Back off after a failed activation attempt instead of retrying
        # the identical order every monitor cycle.
        if now < retry_after.get(sig["signal_id"], 0):
            continue

        # Wait until price re-enters the zone -- unless this is a grid
        # template, whose legs rest AT the zone on the broker's book, so
        # requiring price to already be there before placing them defeats
        # the point. MT5 does the waiting for these.
        #
        # IME gap-fire (2026-08-12, explicit user direction, generalised
        # from the scan path's own gap-adjusted market entry -- see
        # core_scan_messages_auto_execute.execute_auto_signal): a signal
        # that was queued because price had already moved past its zone at
        # first arrival stays queued here forever if price never comes back
        # -- exactly what happened to a GOLD DIGGERS INSTITUTIONAL signal
        # only ~2pt outside its zone. When IME is enabled for this signal's
        # channel, shift entry_low/entry_high/stop_loss/tp1..8 by the same
        # distance price has already moved (preserving the signal's
        # original risk/reward shape from the actual current price) and
        # fall through to activate at market instead of continuing to wait.
        # _pw_updates stays None unless a gap-fire is warranted. CRITICALLY it
        # is NOT written to the database here -- see the deferred write just
        # before open_trade_from_signal below, and _revert_gap_adjust.
        _pw_updates: Optional[dict] = None
        _pw_original: Optional[dict] = None
        if _grid_tpl is None and not price_in_entry_range(
            sig["direction"], float(sig["entry_low"]), float(sig["entry_high"]), tick
        ):
            _pw_ime = _ime_enabled_for_source(rs, sig.get("source_name"))
            _pw_dir = sig["direction"].upper()
            _pw_el, _pw_eh = float(sig["entry_low"]), float(sig["entry_high"])
            _pw_px = tick.ask if _pw_dir == "BUY" else tick.bid
            _pw_gap = (
                round(_pw_px - _pw_eh, 2) if _pw_dir == "BUY"
                else round(_pw_el - _pw_px, 2)
            )
            if not (_pw_ime and _pw_gap > 0):
                continue
            # Distance cap (2026-08-13). Restored after the uncapped version
            # chased a signal 28.22 points (282 pips) past its own zone --
            # at that distance the level the signal was built around is long
            # gone and the "entry" is just buying the top of a move. Beyond
            # the cap the signal keeps waiting for a genuine return to zone.
            # Shared with the fresh-signal scan path so the two cannot drift.
            from backend.src.services.trading.scan_auto_execute import (
                MAX_GAP_FIRE_PTS,
            )
            if _pw_gap > MAX_GAP_FIRE_PTS:
                log.debug(
                    "[PendingWatcher] Signal %s gap %.2f pts exceeds the %.1f pt "
                    "gap-fire cap — staying queued for a real zone return",
                    sig["signal_id"][:8], _pw_gap, MAX_GAP_FIRE_PTS,
                )
                continue

            _pw_sign = 1.0 if _pw_dir == "BUY" else -1.0
            _pw_updates = {
                "entry_low":  round(_pw_el + _pw_sign * _pw_gap, 2),
                "entry_high": round(_pw_eh + _pw_sign * _pw_gap, 2),
                "stop_loss":  round(float(sig["stop_loss"]) + _pw_sign * _pw_gap, 2),
            }
            for _pw_i in range(1, 9):
                _pw_tp = sig.get(f"tp{_pw_i}")
                if _pw_tp is not None:
                    _pw_updates[f"tp{_pw_i}"] = round(float(_pw_tp) + _pw_sign * _pw_gap, 2)
            # Snapshot the pre-shift values so the write can be undone if the
            # activation below does not actually result in an open trade.
            _pw_original = {k: sig.get(k) for k in _pw_updates}
            _pw_log = (sig["signal_id"][:8], _pw_el, _pw_eh, _pw_px, _pw_gap,
                       _pw_updates["stop_loss"])
            # Apply to the in-memory row only, so the remaining gates below
            # (R:R filter, momentum, duplicate guard) score the levels the
            # trade would actually open on rather than the stale pre-shift
            # ones. The database still holds the original until the write
            # immediately before activation succeeds.
            sig = dict(sig)
            sig.update(_pw_updates)

        # Respect the max-trades cap
        if open_count >= max_trades:
            break

        # Pre-trade filters: R:R and directional cap.
        #
        # The bypass list is imported from the fresh-signal scan path rather
        # than kept as a second local copy (2026-08-27). The local copy had
        # drifted: it was missing Scalp Runner, every EA Template, and the
        # IME exemption, so the identical signal that executes on arrival was
        # refused on zone re-entry -- scored against a stop the strategy or
        # template will not use. A queued signal must face the same gate its
        # fresh counterpart faces, or it can only ever sit until it expires.
        #
        # Grid templates skip it for the reason
        # resolve_open_trade_params already exempts every template
        # (core_signal_resolution.py): the levels the trade actually runs on
        # come from the template, not from the signal's own TP1.
        _pw_src = sig.get("source_name") or ""
        _pw_strategy = effective_strategy
        _act_px = tick.ask if sig["direction"].upper() == "BUY" else tick.bid
        _bypass_rr = (
            _grid_tpl is not None
            or _pw_strategy in _PRE_TRADE_FILTER_BYPASS_STRATEGIES
            or ea_templates.is_template_override(_pw_strategy)
            or _ime_enabled_for_source(rs, sig.get("source_name"))
        )
        filter_err = None if _bypass_rr else check_pre_trade_filters(
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
        _existing = signals_repo.find_open_trade_for_signal(sig["signal_id"])
        if _existing:
            signals_repo.mark_signal_activated(sig["signal_id"])
            log.info(
                "[PendingWatcher] Signal %s already has an open trade (%s) — skipping duplicate activation",
                sig["signal_id"][:8], _existing[0][:8],
            )
            continue

        # Momentum confirmation: last completed M5 candle must align with direction.
        # A contrary candle suggests the move into the zone is a fakeout/reversal.
        # Not applied to a grid template: there is no "move into the zone" to
        # confirm yet -- price hasn't reached it -- and deferring on the
        # current candle would just reintroduce the Python-side wait this
        # placement is meant to remove.
        _direction_up = sig["direction"].upper()
        if dpm_candles and _grid_tpl is None:
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

        # Deferred gap-fire write (2026-08-13). Every gate above has now
        # passed, so the shifted levels are finally persisted -- the row this
        # activation is about to read must carry them.
        #
        # Doing this at the point of decision rather than at the point of
        # detection is the whole fix: the original version wrote the shift as
        # soon as it computed one, then fell through to these same gates, and
        # any of them refusing left the shifted values committed. The next
        # cycle re-measured the gap from those already-shifted levels and
        # shifted AGAIN, compounding every second. Live this walked one
        # signal's stop 110 pips over 80 passes (4 signals, 190 passes, all
        # compounding); three of the four expired without ever opening, at
        # levels bearing no relation to what the channel actually sent.
        if _pw_updates is not None:
            signals_repo.apply_signal_levels(sig["signal_id"], _pw_updates)
            log.info(
                "[PendingWatcher] Signal %s gap-adjusted market entry (IME): "
                "zone %.2f-%.2f, market %.2f, gap=%.2f pts -> SL %.2f",
                *_pw_log,
            )

        # Price is back in zone — activate (or, for a grid template, stage the
        # resting legs across the zone without waiting for price at all)
        log.info("[PendingWatcher] Signal %s %s zone $%.2f–$%.2f — %s (age %.0fs)",
                 sig["signal_id"][:8], sig["direction"],
                 float(sig["entry_low"]), float(sig["entry_high"]),
                 "staging grid legs" if _grid_tpl is not None else "activating", age)
        try:
            trade_result = await open_trade_from_signal(
                bridge, sig["signal_id"], tick=tick, age_lot_mult=_age_lot_mult,
                dpm_candles=dpm_candles, starting_balance=starting_balance,
                background_open_commentary=background_open_commentary,
            )
            open_count += 1
            # Flip the TG signal row from 'pending' → 'activated' so the UI updates
            tg_repo.activate_pending_tg_signal(sig["signal_id"])
            if _grid_tpl is not None:
                # A grid ack carries no fill price (ticket 0 / entry 0 -- the
                # EA's own placeholder for "legs staged, none filled yet"), so
                # reporting an entry here would print $0.00.
                _msg = (
                    f"*Grid legs staged*\n"
                    f"{sig['direction']} queued signal handed to the EA — "
                    f"resting legs across "
                    f"${float(sig['entry_low']):.2f}–${float(sig['entry_high']):.2f}\n"
                    f"_Template: {_grid_tpl.get('name', '?')}_"
                )
                _cat = "pending_grid_staged"
            else:
                _msg = (
                    f"*Zone fill activated*\n"
                    f"{sig['direction']} queued signal entered zone "
                    f"${float(sig['entry_low']):.2f}–${float(sig['entry_high']):.2f}\n"
                    f"Opened @ ${trade_result.get('entry_price', '?'):.2f}"
                )
                _cat = "pending_zone_fill"
            asyncio.create_task(telegram_alerts.send_message(
                _msg, sig["signal_id"], _cat,
            ))
            retry_after.pop(sig["signal_id"], None)
            _ACTIVATION_FAILURES.pop(sig["signal_id"], None)
        except Exception as exc:
            _exc_msg = str(exc)
            # Undo the gap-fire write so the next attempt re-measures from the
            # channel's own original levels instead of the shifted ones. This
            # is the second half of the anti-compounding fix -- open_trade_
            # from_signal raising (schedule gate, margin, EA reject, R:R) is
            # exactly the case that previously left a drifted row behind.
            if _pw_updates is not None and _pw_original is not None:
                try:
                    signals_repo.apply_signal_levels(sig["signal_id"], _pw_original)
                    log.info(
                        "[PendingWatcher] Signal %s gap-fire reverted to original "
                        "levels after failed activation",
                        sig["signal_id"][:8],
                    )
                except Exception:
                    log.warning("[PendingWatcher] gap-fire revert failed for %s",
                                sig["signal_id"][:8], exc_info=True)
            retry_after[sig["signal_id"]] = now + _PENDING_ACTIVATION_BACKOFF_S
            # "Expected" refusals are the gates deliberately declining to
            # trade (bad R:R, breaker open, not this node's turn). They cost
            # nothing at the broker and clear on their own, so they must not
            # burn the attempt budget -- only genuine failures do.
            _expected = ("R:R filter" in _exc_msg
                         or "circuit breaker" in _exc_msg.lower()
                         or "stood down" in _exc_msg.lower()
                         or "paused" in _exc_msg.lower())
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

            if _expected:
                continue
            _fails = _ACTIVATION_FAILURES.get(sig["signal_id"], 0) + 1
            _ACTIVATION_FAILURES[sig["signal_id"]] = _fails
            if _fails < _MAX_ACTIVATION_ATTEMPTS:
                continue
            # Give up rather than keep re-attempting until expiry. Each
            # attempt can leave real orders at the broker before it fails,
            # so "retry until the window closes" is not a safe default.
            signals_repo.expire_signal(sig["signal_id"])
            retry_after.pop(sig["signal_id"], None)
            _ACTIVATION_FAILURES.pop(sig["signal_id"], None)
            log.error("[PendingWatcher] Signal %s abandoned after %d failed "
                      "activation attempts — last error: %s",
                      sig["signal_id"][:8], _fails, exc)
            asyncio.create_task(telegram_alerts.send_message(
                (
                    f"*Signal activation abandoned*\n"
                    f"{sig['direction']} zone "
                    f"${float(sig['entry_low']):.2f}–${float(sig['entry_high']):.2f}\n"
                    f"Failed {_fails} times, last error: "
                    f"{telegram_alerts._md_esc(_exc_msg or type(exc).__name__)}\n"
                    f"_Stopped retrying — each attempt can place real orders. "
                    f"Check MT5 for positions this app is not tracking._"
                ),
                sig["signal_id"], "pending_activation_abandoned",
            ))

    return True
