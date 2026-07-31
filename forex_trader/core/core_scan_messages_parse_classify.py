"""Channel-format-based signal parsing/classification block -- extracted
verbatim (no logic changes) from core/engine.py's SimulationEngine._scan_messages
(lines 6601-6807), as part of the core/engine.py migration series. See
docs/todo/refactor/core-scan-messages-parse-classify-migration/020-*.md.

Parsing/classification and DB recording only -- no MT5 order is ever
placed, closed, or modified.

`ai_fallback_fn`/`queue_unrecognised_fn` are required explicit
collaborators bound to the same simplified call shape `_scan_messages`
itself uses -- their real underlying implementations
(`core_ai_signal_fallback.try_ai_signal_fallback`/`queue_unrecognised`)
need additional context (cfg, bridge, is_active_trader_node) only the
caller has, so no real default is supplied here.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.telegram.keyword_triggers import should_skip_ai_fallback_for_no_signal_candidate
from forex_trader.core.signal_parser import (
    parse_gold_signal, parse_gd2_signal, parse_gd2_partial,
    is_gd2_message, is_format_ab_signal, parse_with_learned_rules, _CURRENCY_RE,
    parse_limit_order_signal,
)

log = logging.getLogger(__name__)

AIFallbackFn = Callable[[str, str, str], Awaitable[Optional[dict]]]
QueueUnrecognisedFn = Callable[[str, str, str], None]

_RECENT_DUP_WINDOW = 15 * 60


async def classify_and_parse(
    tg_id: str,
    group_id: str,
    channel_name: str,
    text: str,
    msg: dict,
    parser_fmt: str,
    sig_prefix: str,
    ai_fallback_fn: AIFallbackFn,
    queue_unrecognised_fn: QueueUnrecognisedFn,
    rs: dict,
) -> Optional[dict]:
    """Returns the parsed signal dict (including AI-recovered) if this
    message classified as a signal, or None if fully handled here (dropped,
    queued as unrecognised, recorded unsupported-currency, or recorded as a
    pending_followup partial) -- caller should move to the next message.

    `rs` gates every ai_fallback_fn call below via Logic Keywords'
    symbol_tokens/buy_orders/limit_orders lexicons (see
    core_logic_keyword_triggers.should_skip_ai_fallback_for_no_signal_
    candidate) -- never the deterministic per-format parsers above, which
    keep working exactly as before for every currently-recognised format."""

    async def _gated_ai_fallback(t: str, ch: str, tid: str) -> Optional[dict]:
        _skip = should_skip_ai_fallback_for_no_signal_candidate(t, rs)
        if _skip:
            log.debug("[LogicKeywords] tg_id=%s AI fallback skipped — %s", tid, _skip)
            return None
        return await ai_fallback_fn(t, ch, tid)

    parsed = parse_with_learned_rules(text, channel_name)

    if parsed:
        return parsed

    # Limit Runner's "BUY/SELL [LIMITS] GOLD @ high/low AREA" layout -- any
    # channel, format-matched only, checked ahead of the parser_fmt
    # branching below so a channel configured for format_ab (whose branch
    # never tries GD2-shaped parsing at all) doesn't silently miss it. See
    # core_limit_order_signal.py for what happens with the returned dict's
    # `tp_open` marker.
    _limit_parsed = parse_limit_order_signal(text)
    if _limit_parsed:
        return _limit_parsed

    if parser_fmt == "format_ab":
        _ai_recovered = False
        if not is_format_ab_signal(text, sig_prefix):
            parsed = await _gated_ai_fallback(text, channel_name, tg_id)
            if not parsed:
                return None
            _ai_recovered = True
            cm = None
        else:
            cm = _CURRENCY_RE.search(text)
        if cm and cm.group(1).upper().replace("/", "").replace("-", "") != "XAUUSD":
            currency = cm.group(1).upper()
            msg_ts_str = msg.get("timestamp") or ""
            _is_stale = False
            if msg_ts_str:
                try:
                    from datetime import datetime as _dt
                    _tg_dt = _dt.fromisoformat(msg_ts_str.replace("Z", "+00:00"))
                    if _tg_dt.tzinfo is None:
                        from datetime import timezone as _tz
                        _tg_dt = _tg_dt.replace(tzinfo=_tz.utc)
                    _is_stale = time.time() - _tg_dt.timestamp() > 2 * 3600
                except Exception:
                    pass
            else:
                _is_stale = True
            import re
            _dir_m = re.search(r'\bDirection\s+(BUY|SELL)\b', text, re.IGNORECASE)
            _dir = _dir_m.group(1).upper() if _dir_m else None
            _was_new = False
            _dup_found = False
            with db_module.db() as conn:
                _cur = conn.execute(
                    """INSERT OR IGNORE INTO vantage_tg_signals
                       (tg_message_id,group_id,group_name,sender_name,message_ts,raw_text,parsed_at,
                        direction,entry_low,entry_high,stop_loss,
                        tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
                       VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (tg_id, group_id, channel_name,
                     msg.get("sender_name", ""), msg_ts_str,
                     text, time.time(),
                     _dir, None, None, None,
                     None, None, None, None, None, None, None, None,
                     "unsupported_currency"),
                )
                _was_new = _cur.rowcount > 0
                if _was_new:
                    _recent_rows = conn.execute(
                        """SELECT raw_text FROM vantage_tg_signals
                           WHERE group_id=? AND status='unsupported_currency'
                           AND direction=? AND tg_message_id!=? AND parsed_at>?""",
                        (group_id, _dir, tg_id, time.time() - _RECENT_DUP_WINDOW),
                    ).fetchall()
                else:
                    _recent_rows = []
            _norm_currency = currency.replace("/", "").replace("-", "")
            for (_prior_text,) in _recent_rows:
                _prior_cm = _CURRENCY_RE.search(_prior_text or "")
                if _prior_cm and _prior_cm.group(1).upper().replace("/", "").replace("-", "") == _norm_currency:
                    _dup_found = True
                    break
            log.info("[%s] Non-XAUUSD signal tg_id=%s currency=%s stale=%s dup=%s",
                     channel_name, tg_id, currency, _is_stale, _dup_found)
            if _was_new and not _is_stale and not _dup_found:
                asyncio.create_task(
                    telegram_alerts.send_message(
                        f"Signal received from {channel_name}\n"
                        f"Currency: {currency} — app handles XAUUSD only, not executed.\n"
                        f"Direction: {_dir or '?'}",
                        tg_id, "signal_currency_skipped",
                    )
                )
            return None
        if not _ai_recovered:
            parsed = parse_gold_signal(text)
            if not parsed:
                parsed = await _gated_ai_fallback(text, channel_name, tg_id)
                if not parsed:
                    queue_unrecognised_fn(tg_id, channel_name, text)
                    return None
        return parsed

    elif parser_fmt == "gd2":
        _ai_recovered = False
        if not is_gd2_message(text):
            parsed = await _gated_ai_fallback(text, channel_name, tg_id)
            if not parsed:
                return None
            _ai_recovered = True
        if not _ai_recovered:
            parsed = parse_gd2_signal(text)
        if not parsed:
            _partial = parse_gd2_partial(text)
            if _partial:
                msg_ts_str = msg.get("timestamp") or ""
                with db_module.db() as conn:
                    conn.execute(
                        """INSERT OR IGNORE INTO vantage_tg_signals
                           (tg_message_id,group_id,group_name,sender_name,message_ts,
                            raw_text,parsed_at,direction,entry_low,entry_high,
                            stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
                           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (tg_id, group_id, channel_name,
                         msg.get("sender_name", ""), msg_ts_str,
                         text, time.time(),
                         _partial["direction"],
                         _partial["entry_low"], _partial["entry_high"],
                         None, None, None, None, None, None, None, None, None,
                         "pending_followup"),
                    )
                log.info(
                    "[%s] Partial GD2 tg_id=%s %s entry %s-%s — awaiting SL/TP edit",
                    channel_name, tg_id, _partial["direction"],
                    _partial["entry_low"], _partial["entry_high"],
                )
                return None
            from forex_trader.core.signal_parser import parse_gd2_instant_entry
            _ime_trigger = parse_gd2_instant_entry(text)
            if _ime_trigger:
                log.info(
                    "[%s] GD2 bare direction tg_id=%s (%s) — silently skipped "
                    "(awaiting follow-up with full levels)",
                    channel_name, tg_id, _ime_trigger[0],
                )
                return None
            parsed = await _gated_ai_fallback(text, channel_name, tg_id)
            if not parsed:
                queue_unrecognised_fn(tg_id, channel_name, text)
                return None
        return parsed

    else:
        # 'auto' — try format_ab first (if prefix configured), then gd2
        if sig_prefix and is_format_ab_signal(text, sig_prefix):
            cm = _CURRENCY_RE.search(text)
            if not cm or cm.group(1).upper().replace("/", "").replace("-", "") == "XAUUSD":
                parsed = parse_gold_signal(text)
        if not parsed and is_gd2_message(text):
            parsed = parse_gd2_signal(text)
            if not parsed:
                _partial = parse_gd2_partial(text)
                if _partial:
                    msg_ts_str = msg.get("timestamp") or ""
                    with db_module.db() as conn:
                        conn.execute(
                            """INSERT OR IGNORE INTO vantage_tg_signals
                               (tg_message_id,group_id,group_name,sender_name,message_ts,
                                raw_text,parsed_at,direction,entry_low,entry_high,
                                stop_loss,tp1,tp2,tp3,tp4,tp5,tp6,tp7,tp8,status)
                               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                            (tg_id, group_id, channel_name,
                             msg.get("sender_name", ""), msg_ts_str,
                             text, time.time(),
                             _partial["direction"],
                             _partial["entry_low"], _partial["entry_high"],
                             None, None, None, None, None, None, None, None, None,
                             "pending_followup"),
                        )
                    log.info(
                        "[%s] Partial GD2 tg_id=%s %s entry %s-%s — awaiting SL/TP edit",
                        channel_name, tg_id, _partial["direction"],
                        _partial["entry_low"], _partial["entry_high"],
                    )
                    return None
        if not parsed:
            parsed = await _gated_ai_fallback(text, channel_name, tg_id)
        if not parsed:
            return None  # auto-format channel — skip silently when nothing matches
        return parsed
