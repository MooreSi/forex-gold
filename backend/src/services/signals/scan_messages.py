"""The Telegram message scan pipeline (M4 B9a).

This was SimulationEngine._scan_messages: 314 lines of dedup, logic
keywords, instant entry, SL adjustment, parse, staleness, strategy
resolution, execution and alerting, sitting inline on the runtime where
the only way to run it was to construct an engine. The body below is that
code verbatim -- every branch, comment and log line -- with one mechanical
change: the fifteen `self.X` references became fields on ScanCtx.

Same idiom as the runtime's _make_close_trade_ctx: one authoritative
binding site instead of collaborators reached for ad hoc from inside a
god object. A relocation like this fails by dropping a collaborator on a
branch nobody exercises, so the binding is asserted directly in
tests/core/test_scan_messages_relocation.py rather than left to luck.

One field earns an explanation. `engine_for_eval` is the engine itself,
threaded through to channel_strategy_ai.evaluate_signal_strategy, which
still takes an engine-shaped object. Naming it rather than hiding it
behind `self` is the point: it is the one piece of engine coupling this
pipeline has left, and it is now visible in a dataclass instead of
invisible in a method body.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Optional

import asyncio
import re
import time

from backend.src.services.signals.parser import SIGNAL_PREFIX
from backend.src.utils.models import STRATEGY_LIMIT_RUNNER
from backend.src.utils.models import STRATEGY_NAMES
from backend.src.services.trading.ai_signal_fallback import apply_sl_adjustment as _apply_sl_adjustment_impl
from backend.src.services.signals.scan_parse_classify import classify_and_parse as _classify_and_parse_impl
from backend.src.services.broker import ea_templates as _ea_templates
from backend.src.services.trading.scan_auto_execute import execute_auto_signal as _execute_auto_signal_impl
from backend.src.services.trading.limit_order_signal import handle_limit_order_signal as _handle_limit_order_signal_impl
from backend.src.services.signals.scan_edit_reparse import handle_signal_edit as _handle_signal_edit_impl
from backend.src.services.trading.instant_entry import process_instant_entry as _process_instant_entry_impl
from backend.src.services.signals.scan_staleness import record_staleness_or_new as _record_staleness_or_new_impl
from backend.src.services.signals.scan_staleness import resolve_strategy_and_skip_reason as _resolve_strategy_and_skip_reason_impl
from backend.src.services.signals import tg_repo as _tg_repo
from backend.src.services.signals.parser import check_sl_adjustment_rules
from backend.src.db import database as db_module
from backend.src.services.signals.parser import is_gd2_message
from backend.src.services.signals.parser import parse_gd2_instant_entry
from backend.src.services.signals.parser import parse_instant_entry
from backend.src.services.telegram.keyword_triggers import should_skip_for_exclusion
from backend.src.services.telegram.keyword_triggers import should_skip_media_or_forwarded
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.telegram.keyword_triggers import try_handle_close_all_trigger
from backend.src.services.telegram.keyword_triggers import try_handle_risk_free_be_trigger
from backend.src.services.telegram.keyword_triggers import try_handle_tp_hit_trigger
from backend.src.services.telegram.keyword_triggers import apply_sl_parsing_override


log = logging.getLogger(__name__)


@dataclass
class ScanCtx:
    """Everything the scan pipeline reached for through `self`."""
    bridge: Any = None
    tg_reader: Optional[Any] = None
    cfg: Optional[dict] = None
    dpm_candles: Optional[Any] = None
    # Mutable, and deliberately shared with the runtime rather than copied:
    # the accept_tg_signals-is-OFF warning is throttled to once per five
    # minutes ACROSS scans, so a per-call copy would warn on every cycle.
    tg_off_warn_state: dict = field(default_factory=dict)
    # See the module docstring -- the last engine-shaped dependency.
    engine_for_eval: Any = None
    # Bound runtime methods.
    close_trade: Optional[Callable[..., Awaitable[dict]]] = None
    try_ai_signal_fallback: Optional[Callable[..., Awaitable[Any]]] = None
    find_and_apply_instant_followup: Optional[Callable[..., Awaitable[bool]]] = None
    get_trading_balance: Optional[Callable[..., Awaitable[float]]] = None
    suggest_lot_size: Optional[Callable[..., Any]] = None
    queue_unrecognised: Optional[Callable[..., Awaitable[None]]] = None
    is_trading_paused: Optional[Callable[[], bool]] = None
    get_open_trades: Optional[Callable[[], list]] = None
    check_pre_trade_filters: Optional[Callable[..., Awaitable[Any]]] = None
    open_trade: Optional[Callable[..., Awaitable[dict]]] = None


async def scan_messages(ctx: ScanCtx) -> list[dict]:
    # Centralized signal generation (Settings > Remote Node): once this
    # VPS is the active trader and generation has moved to the Mac, skip
    # parsing/creating GD2/the reference channel/Format-AB signals entirely here rather
    # than just letting a later open_trade() gate discard the work —
    # this is what actually saves the CPU, not just the execution.
    if not await db_module.to_db_thread(db_module.should_generate_signals_here):
        return []
    if ctx.tg_reader is None:
        return []
    msgs = ctx.tg_reader.get_buffer_messages(limit=100)
    if not msgs:
        return []

    slot_groups = ctx.tg_reader.get_active_group_slots()
    rs           = db_module.get_risk_settings()

    if not bool(rs.get("accept_tg_signals", 1)):
        # Dropping signals must never be silent — an accidental toggle-off
        # cost a valid the reference channel signal on 2026-07-03 with zero log evidence.
        # Warn (throttled to once per 5 min) whenever messages are being
        # discarded while the switch is off.
        now_ts = time.time()
        if now_ts - ctx.tg_off_warn_state.get("at", 0.0) > 300:
            ctx.tg_off_warn_state["at"] = now_ts
            log.warning(
                "[engine] accept_tg_signals is OFF — %d buffered Telegram "
                "message(s) are being IGNORED (Trading tab → Signals → "
                "'Telegram Signals' toggle)", len(msgs),
            )
        return []

    auto_execute = bool(rs.get("auto_execute_signals", 0))
    new_signals: list[dict] = []

    exclude_high_risk = bool(rs.get("exclude_high_risk", 0))

    for msg in msgs:
        try:
            tg_id    = str(msg.get("id") or "")
            group_id = str(msg.get("group_id") or "")
            text     = (msg.get("text") or "").strip()
            if not tg_id or not text:
                continue
            from backend.src.utils import latency_trace as _lt_scan
            _lt_scan.mark(tg_id, "t6_scanning")
            if slot_groups and group_id and group_id not in slot_groups:
                continue

            if exclude_high_risk and "high risk" in text.lower():
                log.info("[engine] Skipping high-risk signal tg_id=%s", tg_id)
                continue

            slot         = slot_groups.get(group_id, 1)
            channel_name = ctx.tg_reader.get_group_name(group_id) or f"Channel {slot}"

            # Resolve channel parser config — auto-bootstrap on first sight
            ch_cfg = db_module.get_channel_parser_config(channel_name)
            if ch_cfg is None:
                _default_fmt    = 'format_ab' if slot == 1 else 'gd2'
                _default_prefix = SIGNAL_PREFIX if _default_fmt == 'format_ab' else ''
                _default_ime    = 1  # enabled for both format_ab and gd2
                db_module.save_channel_parser_config(
                    channel_name, _default_fmt, _default_prefix,
                    bool(_default_ime), True,
                    f'Auto-configured from slot {slot}',
                )
                ch_cfg = db_module.get_channel_parser_config(channel_name) or {}
            parser_fmt  = ch_cfg.get('parser_format', 'auto')
            sig_prefix  = ch_cfg.get('signal_prefix') or SIGNAL_PREFIX
            # Default ime_enabled to True for both known formats; only False when
            # the user has explicitly set it to 0 in the channel config.
            _ime_default = 1 if parser_fmt in ('format_ab', 'gd2') else 0
            ime_enabled  = bool(ch_cfg.get('instant_entry_enabled', _ime_default))

            if parser_fmt == 'none' or not bool(ch_cfg.get('enabled', 1)):
                continue

            # ── Logic Keywords (Parsing page) ────────────────────────────────
            # Global, user-editable phrase lexicons -- checked ahead of
            # everything else below since Ignore Media/Forwarded and the
            # CLOSE ALL/RISK FREE-BE/TP HIT triggers are orthogonal to normal
            # signal parsing (a "CLOSE ALL" message, say, would never parse as
            # a signal anyway, but skipping it here avoids it falling through
            # to the unrecognised-message queue). See core_logic_keyword_triggers.py.
            _lk_skip = should_skip_media_or_forwarded(msg, rs)
            if _lk_skip:
                log.debug("[LogicKeywords] tg_id=%s skipped — %s", tg_id, _lk_skip)
                continue
            if await try_handle_close_all_trigger(
                text, channel_name, tg_id, rs, close_trade_fn=ctx.close_trade,
            ):
                continue
            if await try_handle_risk_free_be_trigger(
                text, channel_name, tg_id, rs, bridge=ctx.bridge,
            ):
                continue
            if await try_handle_tp_hit_trigger(text, channel_name, tg_id, rs):
                continue

            # Dedup — skip already-processed messages, but re-parse edited ones
            # if the signal hasn't been executed yet (direction correction).
            _existing = _tg_repo.get_tg_signal_meta(tg_id)
            if _existing:
                _ex = db_module.row_to_dict(_existing)
                _edit_result = await _handle_signal_edit_impl(
                    tg_id, group_id, channel_name, text, parser_fmt, _ex,
                    ai_fallback_fn=ctx.try_ai_signal_fallback,
                    find_and_apply_instant_followup_fn=ctx.find_and_apply_instant_followup,
                    close_trade_fn=ctx.close_trade,
                )
                if _edit_result is None:
                    continue
                # A pending_followup signal just received its SL/TP via this
                # edit — fall through to the normal Instant-Market-Entry /
                # SL-adjustment / classify-and-parse pipeline below exactly
                # like a brand-new message would (the edited raw_text now
                # parses as a full signal on its own); _edit_result itself is
                # deliberately unused past this point, matching the original
                # inline code's behavior of re-deriving `parsed` from scratch
                # rather than reusing the edit-time reparse.

            # ── Instant Market Entry ────────────────────────────────────────
            # format_ab: fires on "XAUUSD Buy Now" / "XAU Sell Now" (requires NOW).
            # gd2: fires on "XAU USD BUY [NOW]" / "XAU USD SELL [NOW]" with or
            #      without "NOW", or the newer "Buy/Sell Zone Now" bare trigger
            #      (see is_gd2_message — a plain "XAU" substring check missed
            #      this format entirely, since none of its messages mention
            #      XAU anywhere, silently disabling IME for GD2 on 2026-07-06
            #      even with the setting on and correctly synced).  GD2
            #      messages that already carry SL/TP are excluded (they go
            #      through parse_gd2_signal as full signals).
            _ime_gate = is_gd2_message(text) if parser_fmt == 'gd2' else "XAU" in text.upper()
            if bool(rs.get("immediate_market_entry", 0)) and _ime_gate and ime_enabled:
                if parser_fmt == 'gd2':
                    _instant = parse_gd2_instant_entry(text)
                else:
                    _instant = parse_instant_entry(text)
                if _instant:
                    _instant_dir, _instant_px = _instant
                    await _process_instant_entry_impl(msg, tg_id, group_id, channel_name, text, _instant_dir, _instant_px, rs, auto_execute, ctx.bridge, ctx.dpm_candles, ctx.cfg.get('starting_balance', 1000.0))
                    continue

            # ── SL-adjustment fast path (learned rule only — no AI call) ────
            # Checked before any entry-signal parsing since it's an orthogonal
            # message category (a follow-up to an EXISTING trade, e.g. "Adjust
            # SL to 4060", not a new entry) — see signal_parser.
            # check_sl_adjustment_rules and _apply_sl_adjustment above. A
            # message with no matching rule yet still reaches the entry-signal
            # gates below and, if those also fail, the AI fallback classifies
            # it there instead (first-time cost; a rule gets built on approval
            # so it never needs another AI call for this channel's wording).
            _sl_adj = check_sl_adjustment_rules(text, channel_name)
            if _sl_adj is not None:
                await _apply_sl_adjustment_impl(_sl_adj, channel_name, tg_id, 'learned_rule', ctx.bridge)
                continue

            # ── Logic Keywords: exclusion pre-check ──────────────────────────
            # Gates only the new-signal-parsing path below (unlike the
            # triggers above, which run regardless) -- see
            # should_skip_for_exclusion's own docstring for why this doesn't
            # also gate on symbol_tokens.
            _lk_parse_skip = should_skip_for_exclusion(text, rs)
            if _lk_parse_skip:
                log.debug("[LogicKeywords] tg_id=%s skipped — %s", tg_id, _lk_parse_skip)
                continue

            # ── Channel-name-based signal parsing ────────────────────────────
            source_label = channel_name
            parsed = await _classify_and_parse_impl(
                tg_id, group_id, channel_name, text, msg, parser_fmt, sig_prefix,
                ai_fallback_fn=ctx.try_ai_signal_fallback,
                queue_unrecognised_fn=ctx.queue_unrecognised,
                rs=rs,
            )
            if parsed is None:
                continue

            # ── Logic Keywords: Enable TP/SL Parsing toggles ─────────────────
            # OFF means "don't use this signal's own stated TP/SL levels" --
            # strips the field(s) right after parsing so every downstream
            # consumer (validation, strategy resolution, execution) sees
            # exactly the same "missing field" shape it already handles for
            # a signal that never had one, rather than a new code path.
            if not bool(rs.get("lk_enable_tp_parsing", 1)):
                for _tp_i in range(1, 9):
                    parsed[f"tp{_tp_i}"] = None
            # A stop is NOT optional the way a TP is, so SL Parsing OFF substitutes
            # a configured distance rather than stripping the field. Stripping it
            # (what this did before the 2026-08-25 merge) sent the signal on with
            # no stop at all -- see apply_sl_parsing_override for the resolution
            # order (channel template sl_pips, then Fallback SL Distance, then the
            # built-in default) and for the live crash stripping caused.
            _sl_sub = apply_sl_parsing_override(parsed, rs, channel_name)
            if _sl_sub:
                log.info("[LogicKeywords] tg_id=%s — %s", tg_id, _sl_sub)

            # Staleness guard — signals are scalps: an entry zone is only valid for
            # minutes. Anything older than 4 minutes at processing time is recorded
            # as historical and never executed. The previous 2-hour window let a
            # 22-minute-old the reference channel signal execute at market after a downtime
            # (2026-07-03: toggle-off gap → backfilled signal filled 22min late at
            # a worse price → straight to SL). 4 min covers Telegram delivery
            # latency plus one scan cycle, nothing more.
            _is_fresh = await _record_staleness_or_new_impl(
                tg_id, group_id, channel_name, msg, parsed, source_label,
            )
            if not _is_fresh:
                continue

            from backend.src.utils import latency_trace as _lt_dec
            _lt_dec.mark(tg_id, "t7_decided")

            if parsed.get("_ai_extracted"):
                # The deterministic parser missed this message (format drift) —
                # log it for the review tab; the Telegram notification is
                # merged into the fmt_signal() call below (single message
                # instead of two separate ones hitting the user's phone).
                log.info(
                    "[AI-Fallback] tg_id=%s executing an AI-recovered signal "
                    "(confidence=%.2f)", tg_id, parsed.get("_ai_confidence", 0),
                )

            executed             = False
            exec_lot             = None
            exec_price           = None
            trade_result         = None
            _gap_note            = ""  # only set below if gap-adjusted execution fires

            # Channel override > auto-Claude rec > global Active Strategy,
            # plus the per-signal "High Risk" override, session gate, and
            # trading-paused check.
            _strat_result = await _resolve_strategy_and_skip_reason_impl(
                rs, channel_name, text, parsed, auto_execute,
                ctx.cfg, ctx.engine_for_eval,
                is_trading_paused_fn=lambda: db_module.to_db_thread(ctx.is_trading_paused),
            )
            strategy             = _strat_result["strategy"]
            strategy_name        = _strat_result["strategy_name"]
            skip_reason          = _strat_result["skip_reason"]
            _sess_ok             = _strat_result["sess_ok"]
            _per_signal_skip     = _strat_result["per_signal_skip"]
            _per_signal_skip_rsn = _strat_result["per_signal_skip_reason"]

            if auto_execute:
                # Limit Runner: a genuine EA pending order, not a market-fill
                # signal — parse_limit_order_signal's `tp_open` marker (always
                # present, True or False, only on its own return dicts) means
                # this signal would default to Limit Runner regardless of
                # whatever channel-override/active-strategy _resolve_strategy_
                # and_skip_reason_impl above just computed, since the strategy
                # here is format-triggered, not channel-configured — EXCEPT
                # when the channel has an EA Template assigned (Trading >
                # Strategy > Channel Strategy > "Template: <name>"): a
                # template fully replaces strategy dispatch by design (see
                # core_ea_templates.py's module docstring), so it must win
                # over the format-triggered default too, not just over the
                # built-in strategies. Fixed 2026-07-24 — a template-assigned
                # channel's "[LIMITS]"-formatted signals were silently always
                # executing as Limit Runner instead, so a configured Grid
                # template never actually ran for them.
                if parsed.get("tp_open") is not None and not _ea_templates.is_template_override(strategy):
                    strategy      = STRATEGY_LIMIT_RUNNER
                    strategy_name = STRATEGY_NAMES[STRATEGY_LIMIT_RUNNER]
                    _exec_result = await _handle_limit_order_signal_impl(
                        parsed, tg_id, channel_name, source_label, rs,
                        _sess_ok, _per_signal_skip, _per_signal_skip_rsn, skip_reason,
                        get_trading_balance_fn=ctx.get_trading_balance,
                        suggest_lot_size_fn=ctx.suggest_lot_size,
                        bridge=ctx.bridge,
                    )
                    executed     = False
                    exec_lot     = None
                    exec_price   = None
                    trade_result = None
                    skip_reason  = _exec_result["skip_reason"]
                else:
                    _exec_result = await _execute_auto_signal_impl(
                        parsed, tg_id, channel_name, source_label, strategy, rs,
                        _sess_ok, _per_signal_skip, _per_signal_skip_rsn, skip_reason,
                        ctx.bridge,
                        get_open_trades_fn=ctx.get_open_trades,
                        find_and_apply_instant_followup_fn=ctx.find_and_apply_instant_followup,
                        check_pre_trade_filters_fn=ctx.check_pre_trade_filters,
                        suggest_lot_size_fn=ctx.suggest_lot_size,
                        get_trading_balance_fn=ctx.get_trading_balance,
                        open_trade_fn=ctx.open_trade,
                    )
                    executed     = _exec_result["executed"]
                    exec_lot     = _exec_result["exec_lot"]
                    exec_price   = _exec_result["exec_price"]
                    trade_result = _exec_result["trade_result"]
                    skip_reason  = _exec_result["skip_reason"]
                    _gap_note    = _exec_result["gap_note"]
                    if _exec_result.get("followup_matched") or _exec_result.get("deferred_stood_down"):
                        new_signals.append(parsed | {"tg_message_id": tg_id, "auto_executed": executed,
                                                     "source_label": source_label})
                        continue

            # Forwarded trade (centralized signal generation): the VPS actually
            # placed it — trade_id/mt5_ticket belong to its DB, not this node's
            # — and its own _handle_signal_order already schedules the correct
            # "Node: Remote" notification via _background_open_commentary. This
            # node sending its own fmt_signal alert here as well would always
            # read "Node: Local" (_node_label() reflects this machine, not
            # which one executed the trade) — a duplicate, misleading alert
            # claiming local execution for a trade the VPS handled.
            if not (trade_result and trade_result.get("executed_remotely")):
                _alert_strategy = (
                    f"{strategy_name} | {_gap_note}" if _gap_note else strategy_name
                )
                alert = telegram_alerts.fmt_signal(
                    parsed, channel_name, executed,
                    exec_lot, exec_price, skip_reason, _alert_strategy,
                    ai_confidence=parsed.get("_ai_confidence") if parsed.get("_ai_extracted") else None,
                    ai_reasoning=parsed.get("_ai_reasoning", "") if parsed.get("_ai_extracted") else "",
                )
                asyncio.create_task(telegram_alerts.send_message(alert, event_type="tg_signal_detected"))
            new_signals.append(parsed | {"tg_message_id": tg_id, "auto_executed": executed,
                                         "source_label": source_label})

        except Exception as _msg_exc:
            # One malformed message must not abandon the whole scan pass.
            # Ported from upstream engine.py by the 2026-08-25 merge: this
            # branch forked before the guard existed, so any error here
            # propagated out of scan_messages and every later message in
            # the buffer went unparsed.
            log.exception("Signal scan failed for tg_id=%s (channel=%s) — "
                          "skipping this message, continuing the batch: %s",
                          msg.get("id"), msg.get("group_id"), _msg_exc)
            continue
    return new_signals
