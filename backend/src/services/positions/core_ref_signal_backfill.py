"""REF signal backfill (2026-07-31).

vantage_tg_signals is only ever written by engine.py's _scan_messages, which
runs on live buffered messages and is gated on accept_tg_signals. So any
window where the app was down, or where that toggle was off, leaves a
permanent hole: the raw messages are still in telegram_messages, but nothing
ever turns them into signal rows.

That hole is not cosmetic. reversal_engine_correlate reads vantage_tg_signals
to match the Reversal Engine's own signals against the professional channels'
entries, and REF-correlated signals are the only subset of that engine's
trades which does not lose money. Found live 2026-07-31: three consecutive
days of ref_signals_sent=0 while 107 perfectly parseable entry signals sat
unused in telegram_messages.

This module reparses those stored messages after the fact and records what
they were.

**It never executes anything.** It only INSERTs rows, with status
'historical' -- the status _record_staleness_or_new_impl already uses for a
signal recorded too late to trade -- and with parsed_at set from the
message's own timestamp rather than now, so correlation time-deltas stay
truthful instead of making every backfilled signal look like it arrived this
second. Execution happens inline in _scan_messages and is never driven by
reading this table, so a row here cannot open a trade.

Deliberately deterministic-parsers-only: no AI fallback (this runs over
hundreds of historical messages and would burn an AI call on each miss) and
no unrecognised-message queueing (that queue is for live review, and
retroactively filling it with days-old messages would bury genuine items).
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.positions import repo as positions_repo
from backend.src.services.signals.parser import (
    is_format_ab_signal, is_gd2_message, parse_gd2_signal, parse_gold_signal,
    parse_limit_order_signal, _CURRENCY_RE,
)

log = logging.getLogger(__name__)

_DEFAULT_LOOKBACK_H = 72
_DEFAULT_LIMIT = 2000
_BACKFILL_STATUS = "historical"


def _to_epoch(value) -> Optional[float]:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (TypeError, ValueError):
        return None


def parse_stored_message(text: str, parser_fmt: str, sig_prefix: str = "") -> Optional[dict]:
    """Deterministic-only reparse of one stored message. Mirrors the parser
    selection classify_and_parse makes, minus every side effect: no DB write,
    no AI fallback, no unrecognised queueing, no partial/pending_followup
    handling (a partial has no levels to correlate on, so recording one here
    would add a row that helps nothing).

    `parser_fmt` is kept in the signature because callers read it from the
    channel config, and because 'none'/disabled still means a channel is not
    scanned; it no longer selects which parsers a message is measured
    against. See the comment below."""
    if not text:
        return None

    # Checked first, exactly as classify_and_parse does, so a channel
    # configured for format_ab doesn't miss the "[LIMITS] GOLD @ x/y AREA"
    # layout its own branch would never try.
    parsed = parse_limit_order_signal(text)
    if parsed:
        return parsed

    # One pipeline for every channel, exactly as classify_and_parse does
    # since the 2026-08-27 owner directive. `parser_fmt` is deliberately not
    # read: it decides whether a channel is scanned at all and the default
    # for its Immediate Market Entry flag, and nothing else. Until
    # 2026-09-16 this function still branched three ways on it -- a month
    # after the live path stopped -- so a well-formed signal in the "wrong"
    # layout for its channel was recorded live and missed here, on a table
    # whose whole purpose is to be the record of what the channels said
    # (docs/todo/bugs/061).
    looks_like_signal = (is_format_ab_signal(text, sig_prefix)
                         or is_gd2_message(text))

    # Gated on the message being signal-shaped so ordinary chat naming a
    # pair is not refused as a foreign-currency signal -- the live path's
    # own condition.
    #
    # Defence in depth, and currently unreachable: `parse_gold_signal`
    # refuses a non-XAUUSD currency line itself, and `is_gd2_message`
    # requires XAUUSD in the trigger, so no message that gets this far can
    # fail here. A mutant deleting these three lines survives the suite, and
    # that is a true result rather than a missing test -- the only input that
    # reaches them is self-contradictory (a GD2 "XAUUSD BUY NOW" carrying a
    # "Currency: EURUSD" line). It stays because the live path has it and
    # this function's whole job is to not diverge from the live path; if
    # either parser's own currency check is ever relaxed, this is what keeps
    # the two ends together.
    cm = _CURRENCY_RE.search(text) if looks_like_signal else None
    if cm and cm.group(1).upper().replace("/", "").replace("-", "") != "XAUUSD":
        return None

    if is_format_ab_signal(text, sig_prefix):
        parsed = parse_gold_signal(text)
        if parsed:
            return parsed
    if is_gd2_message(text):
        return parse_gd2_signal(text)
    return None


def backfill_ref_signals(lookback_hours: int = _DEFAULT_LOOKBACK_H,
                         limit: int = _DEFAULT_LIMIT) -> dict:
    """Reparses stored messages from the last `lookback_hours` and records any
    that are signals but have no vantage_tg_signals row yet.

    Returns {"scanned", "recorded", "already_present", "unparseable"} for
    logging."""
    cutoff_epoch = time.time() - lookback_hours * 3600
    cutoff_iso = datetime.fromtimestamp(cutoff_epoch, tz=timezone.utc).isoformat()

    scanned = recorded = unparseable = 0
    try:
        rows = positions_repo.fetch_unparsed_telegram_messages(cutoff_iso, limit)

        # Channel parser formats, read once rather than per message.
        cfgs: dict[str, dict] = {}
        for r in rows:
            name = r["group_name"] or ""
            if name not in cfgs:
                cfgs[name] = db_module.get_channel_parser_config(name) or {}

        records = []
        for r in rows:
            scanned += 1
            # One malformed message must not abandon the whole pass. The live
            # scan has had this guard since the 2026-08-25 merge
            # (scan_messages.py); this module was written after it and did
            # not, so its only try/except was around the entire loop AND the
            # single INSERT that follows it. A "Buy Gold Now" from Gold
            # Diggers Scalping on 2026-09-14 15:30 typed its entry as
            # "4284.5. - 4278.5"; _GD2_ENTRY_RANGE_RE captured the full stop
            # and parser._f raised on it, and every hourly pass from then on
            # threw away the rows it had already built -- 15 parseable REF
            # signals in the window, none recorded, once an hour, visible
            # only as a one-line WARNING (docs/todo/bugs/061).
            try:
                cfg = cfgs.get(r["group_name"] or "", {})
                parsed = parse_stored_message(
                    r["text"],
                    cfg.get("parser_format", "auto"),
                    cfg.get("signal_prefix", "") or "",
                )
                if not parsed:
                    continue

                # The message's own time, not now -- correlation compares this
                # against the Reversal Engine signal's created_at to decide
                # who fired first, so a wrong value here silently corrupts
                # every lead/lag measurement it produces.
                parsed_at = (_to_epoch(r["timestamp"])
                             or _to_epoch(r["received_at"])
                             or cutoff_epoch)

                records.append((
                    str(r["telegram_message_id"]), str(r["group_id"]), r["group_name"],
                    r["sender_name"] or "", str(r["timestamp"] or ""), r["text"],
                    parsed_at, parsed.get("direction"),
                    parsed.get("entry_low"), parsed.get("entry_high"),
                    parsed.get("stop_loss"),
                    *[parsed.get(f"tp{i}") for i in range(1, 9)],
                    _BACKFILL_STATUS,
                ))
            except Exception as msg_exc:                # noqa: BLE001
                unparseable += 1
                log.warning("[RefBackfill] skipping tg_id=%s (channel=%s), "
                            "continuing the pass: %s",
                            r["telegram_message_id"], r["group_name"], msg_exc)
                continue

        recorded = positions_repo.insert_backfilled_signals(records)
    except Exception as exc:
        log.warning("[RefBackfill] failed: %s", exc)
        return {"scanned": scanned, "recorded": recorded, "already_present": 0,
                "unparseable": unparseable}

    if recorded:
        log.info("[RefBackfill] recorded %d previously-unparsed REF signal(s) "
                 "from %d stored messages in the last %dh (status=%s, not executable)",
                 recorded, scanned, lookback_hours, _BACKFILL_STATUS)
    else:
        log.debug("[RefBackfill] nothing to backfill (%d messages checked)", scanned)
    return {"scanned": scanned, "recorded": recorded,
            "already_present": scanned - recorded,
            "unparseable": unparseable}
