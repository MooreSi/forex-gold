"""Signal parsing/classification for an incoming Telegram message.

Originally extracted verbatim from core/engine.py's
SimulationEngine._scan_messages (lines 6601-6807) -- see
docs/todo/refactor/core-scan-messages-parse-classify-migration/020-*.md.

Reshaped 2026-08-27: the three-way branch on the channel's configured
`parser_format` is gone. Every channel now runs the same parsers in the
same order. The branching meant a signal in the "wrong" layout for its
channel was never measured against the parser that would have read it --
a Format A/B channel never tried a single GD2 regex and vice versa.

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
import hashlib
import logging
import time
from collections import OrderedDict
from typing import Any, Awaitable, Callable, Optional

from backend.src.db import database as db_module
from backend.src.services.signals import repo as signals_repo
from backend.src.services.signals import tg_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.telegram.keyword_triggers import should_skip_ai_fallback_for_no_signal_candidate
from backend.src.services.positions.core_second_message_merge import (
    attach_followup, hold_or_resolve, is_enabled as second_message_enabled,
)
from backend.src.services.signals.parser import (
    parse_gold_signal, parse_gd2_signal, parse_gd2_partial,
    is_gd2_message, is_format_ab_signal, parse_with_learned_rules, _CURRENCY_RE,
    parse_limit_order_signal, parse_partial_any_format, parse_tp_sl_only,
    parse_format_ab_partial,
)
from backend.src.services.risk import expert_params

log = logging.getLogger(__name__)

AIFallbackFn = Callable[[str, str, str], Awaitable[Optional[dict]]]
QueueUnrecognisedFn = Callable[[str, str, str], None]

def recent_dup_window() -> int:
    """Duplicate-signal suppression window. Was a 15-minute constant; now
    Settings > Expert Tunables."""
    return expert_params.get("duplicate_window_s")


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

    # ── TP/SL in Second Message ──────────────────────────────────────────
    # Ahead of every parser below: a levels-only follow-up names no
    # direction and no entry, so none of them would recognise it anyway,
    # and it must be consumed here rather than falling through to the AI
    # fallback / unrecognised queue. The bare entry it completes is picked
    # up on the held message's next re-scan (see core_second_message_merge).
    if second_message_enabled(rs):
        _followup = parse_tp_sl_only(text)
        if _followup and attach_followup(channel_name, _followup):
            return None
        _partial_any = parse_partial_any_format(text)
        if _partial_any:
            return hold_or_resolve(tg_id, channel_name, _partial_any, rs)

    # ── Already known to be a bare direction (bugs/015) ──────────────────
    # Same message, same text, already classified as a trigger with no levels.
    # Every parser below would reach the same conclusion again, and the scan
    # loop asks about once a second for as long as the message stays in the
    # reader's fetch window -- 8,319 times for one 15-character SELL on
    # 2026-08-28, and still climbing when it was found.
    #
    # Deliberately BELOW the learned-rules parser and the second-message
    # block: the operator can add a learned rule at any moment and it must
    # apply to a message already parked, and a levels-only follow-up is a
    # different message that still has to be consumed.
    #
    # In-process memory, not a database row. 015 proposed parking the message
    # as a `vantage_tg_signals` row; checking the code first showed that would
    # be wrong twice over -- the follow-up matcher reads
    # `vantage_second_message_holds`, not this table, so a parked row helps it
    # not at all; and scan_messages.py routes any message that HAS a row into
    # the edit handler, where an edit adding full levels to a
    # non-pending_followup row updates the fields and returns without
    # executing. Parking would turn a taken trade into a missed one. See the
    # open decision in docs/simon-handover/.
    if _is_known_bare_direction(tg_id, text):
        return None

    # Limit Runner's "BUY/SELL [LIMITS] GOLD @ high/low AREA" layout --
    # every channel, format-matched only. See core_limit_order_signal.py for
    # what happens with the returned dict's `tp_open` marker.
    _limit_parsed = parse_limit_order_signal(text)
    if _limit_parsed:
        return _limit_parsed

    # ── One parsing pipeline for every channel (2026-08-27) ─────────────
    # This used to branch three ways on the channel's configured
    # `parser_format`: a "format_ab" channel never tried a single GD2-shaped
    # parser and a "gd2" channel never tried Format A/B, so a well-formed
    # signal in the "wrong" layout for its channel was dropped on the floor
    # -- AI fallback at best, silence at worst. Only the third branch
    # ('auto') ever tried both. Owner directive: parsing rules apply exactly
    # the same to every Telegram channel.
    #
    # `parser_fmt` is deliberately no longer read here. It still decides two
    # things elsewhere and they are unaffected: 'none'/disabled stops the
    # channel being scanned at all (scan_messages.py), and it sets the
    # DEFAULT for that channel's Immediate Market Entry flag. What it must
    # not do is decide which regexes a message is allowed to be measured
    # against.
    _looks_like_signal = is_format_ab_signal(text, sig_prefix) or is_gd2_message(text)

    # Currency guard. Was inside the format_ab branch only, so a non-XAUUSD
    # signal arriving on a gd2-configured channel was never recorded or
    # reported. Still gated on the message being signal-shaped, so ordinary
    # chat that happens to name a pair does not raise an alert.
    cm = _CURRENCY_RE.search(text) if _looks_like_signal else None
    if cm and cm.group(1).upper().replace("/", "").replace("-", "") != "XAUUSD":
        return _record_unsupported_currency(
            tg_id, group_id, channel_name, text, msg, cm.group(1).upper(),
        )

    # Deterministic parsers, in one fixed order, for every channel.
    parsed = None
    if is_format_ab_signal(text, sig_prefix):
        parsed = parse_gold_signal(text)
    if not parsed and is_gd2_message(text):
        parsed = parse_gd2_signal(text)
    if parsed:
        return parsed

    # Direction + entry with the levels still to come. Recorded as a
    # pending_followup so the Telegram edit / second message that carries
    # SL/TP can complete it. Format A/B's own partial parser is tried
    # alongside GD2's -- pairing them the same way parse_partial_any_format
    # does -- since a format_ab channel previously had no partial path at
    # all and its split signals were simply lost.
    _partial = parse_gd2_partial(text) or parse_format_ab_partial(text)
    if _partial:
        msg_ts_str = msg.get("timestamp") or ""
        tg_repo.insert_tg_signal_if_new(
            tg_id, group_id, channel_name, msg.get("sender_name", ""),
            msg_ts_str, text, _partial, "pending_followup",
        )
        log.info(
            "[%s] Partial signal tg_id=%s %s entry %s-%s — awaiting SL/TP",
            channel_name, tg_id, _partial["direction"],
            _partial["entry_low"], _partial["entry_high"],
        )
        return None

    # A bare direction trigger with no entry at all ("XAU USD BUY",
    # "Buy Zone Now", or whatever the user has typed into Parsing > Logic
    # Keywords' BUY/SELL Orders boxes). Immediate Market Entry has already
    # had its chance at this message upstream; if it did not take it there
    # is nothing to execute yet, so stay quiet rather than queue it as
    # unrecognised.
    from backend.src.services.signals.parser import parse_gd2_instant_entry
    from backend.src.services.telegram.keyword_triggers import (
        parse_lexicon_direction_trigger,
    )
    _ime_trigger = parse_gd2_instant_entry(text) or parse_lexicon_direction_trigger(text)
    if _ime_trigger:
        if _note_bare_direction(tg_id, text):
            log.info(
                "[%s] Bare direction tg_id=%s (%s) — silently skipped "
                "(awaiting follow-up with full levels)",
                channel_name, tg_id, _ime_trigger[0],
            )
        return None

    # Nothing deterministic matched. AI fallback, then the unrecognised
    # queue -- but only for a message that looked like a signal in the first
    # place, so a chatty channel does not fill the queue with noise.
    parsed = await _gated_ai_fallback(text, channel_name, tg_id)
    if parsed:
        return parsed
    if _looks_like_signal:
        queue_unrecognised_fn(tg_id, channel_name, text)
    return None


# ── Bare-direction log suppression (bugs/015) ────────────────────────────────
# This branch is the one terminal path in classify_and_parse that records
# nothing, so nothing marks the message as seen and the scan loop handles it
# again roughly once a second for as long as it stays in the reader's fetch
# window. One 15-character message produced 8,319 identical lines in under
# three hours on 2026-08-28 and was still going.
#
# This suppresses the repeated LOGGING only. The message is still re-parsed
# every cycle -- recording it would change signal-parsing behaviour and is the
# owner's call (see docs/todo/bugs/015). An operator wants to see this once.
#
# Insertion-ordered and bounded: this is module state in a process that runs
# for weeks, and eviction must drop the OLDEST, since dropping the newest would
# restore the every-cycle spam for the message currently in the window.
#
# Keyed on the id AND the body. A Telegram edit keeps the message id and
# changes the text, and that is the usual way a bare direction becomes a real
# signal -- remembering the id alone would skip the edited text forever and
# lose the trade. The body is stored as a short digest so a long message
# cannot make this memory large.
_BARE_LOG_MEMORY = 512
_bare_direction_logged: "OrderedDict[tuple[str, str], None]" = OrderedDict()


def _bare_key(tg_id: str, text: str) -> tuple[str, str]:
    digest = hashlib.blake2s(text.encode("utf-8", "replace"), digest_size=8)
    return (str(tg_id), digest.hexdigest())


def _is_known_bare_direction(tg_id: str, text: str) -> bool:
    """Has this exact message body already been classified as a bare
    direction? Read-only -- it must not record anything, or the first
    sighting would suppress its own log line."""
    return _bare_key(tg_id, text) in _bare_direction_logged


def _note_bare_direction(tg_id: str, text: str) -> bool:
    """True the first time this message body is seen, False on every rescan."""
    key = _bare_key(tg_id, text)
    if key in _bare_direction_logged:
        return False
    _bare_direction_logged[key] = None
    while len(_bare_direction_logged) > _BARE_LOG_MEMORY:
        _bare_direction_logged.popitem(last=False)
    return True


def reset_bare_direction_log_memory() -> None:
    """Test seam. Module state would otherwise leak between tests."""
    _bare_direction_logged.clear()


def _record_unsupported_currency(
    tg_id: str, group_id: str, channel_name: str, text: str, msg: dict,
    currency: str,
) -> None:
    """Record (and, when it is new, fresh and not a repeat, announce) a
    signal for a pair this app does not trade. Extracted unchanged from the
    format_ab branch of classify_and_parse when that branching was removed.
    Always returns None -- the caller treats this message as handled."""
    import re as _re

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
    _dir_m = _re.search(r'\bDirection\s+(BUY|SELL)\b', text, _re.IGNORECASE)
    _dir = _dir_m.group(1).upper() if _dir_m else None
    _dup_found = False
    _was_new, _recent_rows = tg_repo.record_unsupported_currency(
        tg_id, group_id, channel_name,
        msg.get("sender_name", ""), msg_ts_str, text,
        _dir, recent_dup_window(),
    )
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
