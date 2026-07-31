"""Dedup/edit-correction state machine -- extracted verbatim (no logic
changes) from core/engine.py's SimulationEngine._scan_messages
(lines 6330-6561), as part of the core/engine.py migration series. See
docs/todo/refactor/core-scan-messages-edit-reparse-migration/020-*.md.

Fires whenever a Telegram message's tg_message_id already has a
vantage_tg_signals row (Telegram delivers an edit as the same message ID
with new text). Can flatten (close) a real open position via
`close_trade_fn` when an instant-entry edit flips direction.

`ai_fallback_fn`/`find_and_apply_instant_followup_fn`/`close_trade_fn` are
required explicit collaborators bound to the same simplified call shape
`_scan_messages` itself uses (`self._try_ai_signal_fallback(text,
channel_name, tg_id)` etc.) -- their own underlying implementations
(`core_ai_signal_fallback.try_ai_signal_fallback`,
`core_instant_followup.find_and_apply_instant_followup`,
`core_close_trade.close_trade`) need additional context (cfg, bridge,
is_active_trader_node, a CloseTradeContext) that only the caller has, so
no real default is supplied here -- the caller is expected to bind them.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable, Callable, Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.signals.parser import (
    parse_gold_signal, parse_gd2_signal, parse_instant_entry, parse_gd2_instant_entry,
)

log = logging.getLogger(__name__)

_INSTANT_STATUSES = ("instant_activated", "instant_pending", "instant_historical")

AIFallbackFn = Callable[[str, str, str], Awaitable[Optional[dict]]]
FindApplyInstantFollowupFn = Callable[[str, str, dict, str], Awaitable[bool]]
CloseTradeFn = Callable[[str, str], Awaitable[dict]]


async def handle_signal_edit(
    tg_id: str,
    group_id: str,
    channel_name: str,
    text: str,
    parser_fmt: str,
    existing: dict,
    ai_fallback_fn: AIFallbackFn,
    find_and_apply_instant_followup_fn: FindApplyInstantFollowupFn,
    close_trade_fn: CloseTradeFn,
) -> Optional[dict]:
    """Returns None if the message was fully handled here (dedup, in-place
    correction, warning, or dropped) -- caller should move on to the next
    message. Returns the parsed signal dict if this edit promotes the
    message to the normal execution flow (a pending_followup signal that
    just received its SL/TP via the edit)."""
    _ex_status = existing.get("status", "")
    _ex_dir = (existing.get("direction") or "").upper()
    _ex_text = existing.get("raw_text") or ""
    _ex_fields_null = existing.get("entry_low") is None
    if text == _ex_text:
        return None

    if parser_fmt == "gd2":
        _reparse = parse_gd2_signal(text)
    else:
        _reparse = parse_gold_signal(text) or parse_gd2_signal(text)

    if not _reparse:
        if _ex_status in _INSTANT_STATUSES:
            if parser_fmt == "gd2":
                _instant_fix = parse_gd2_instant_entry(text)
            else:
                _instant_fix = parse_instant_entry(text)
            if _instant_fix:
                _fix_dir = _instant_fix[0].upper()
                if _fix_dir != _ex_dir:
                    log.warning(
                        "[%s] INSTANT EDIT CORRECTION tg_id=%s: %s → %s "
                        "(status=%s) — flattening open position",
                        channel_name, tg_id, _ex_dir, _fix_dir, _ex_status,
                    )
                    with db_module.db() as _conn:
                        _irow = _conn.execute(
                            "SELECT * FROM vantage_simulated_trades "
                            "WHERE status='open' AND tg_source IN (?,?) "
                            "ORDER BY open_time DESC LIMIT 1",
                            (channel_name, f"instant:{channel_name}")
                        ).fetchone()
                        _itrade = db_module.row_to_dict(_irow) if _irow else None
                    _flat_note = "no matching open trade found — nothing to close"
                    if (_itrade and
                            _itrade.get("direction", "").upper() == _ex_dir):
                        try:
                            await close_trade_fn(
                                _itrade["trade_id"],
                                f"instant_edit_flip:{_ex_dir}->{_fix_dir}",
                            )
                            _flat_note = f"closed trade {_itrade['trade_id'][:8]}"
                        except Exception as _flat_exc:
                            _flat_note = f"close FAILED: {_flat_exc}"
                            log.error(
                                "[%s] INSTANT EDIT CORRECTION tg_id=%s: "
                                "failed to flatten %s: %s",
                                channel_name, tg_id, _itrade["trade_id"][:8], _flat_exc,
                            )
                    with db_module.db() as conn:
                        conn.execute(
                            "UPDATE vantage_tg_signals SET direction=?, raw_text=? "
                            "WHERE tg_message_id=?",
                            (_fix_dir, text, tg_id),
                        )
                    _alert_txt = (
                        f"INSTANT SIGNAL CORRECTED via edit\n"
                        f"Channel: {channel_name}\n"
                        f"{_ex_dir} → {_fix_dir}\n"
                        f"Action: {_flat_note}\n"
                        f"No new position opened automatically — review and "
                        f"re-enter manually if still valid."
                    )
                    asyncio.create_task(
                        telegram_alerts.send_message(
                            _alert_txt, tg_id, "instant_edit_corrected"
                        )
                    )
                else:
                    with db_module.db() as conn:
                        conn.execute(
                            "UPDATE vantage_tg_signals SET raw_text=? "
                            "WHERE tg_message_id=?",
                            (text, tg_id),
                        )
                return None
        _ai_edit = await ai_fallback_fn(text, channel_name, tg_id)
        if _ai_edit:
            _reparse = _ai_edit
        else:
            log.debug(
                "[%s] Edit to tg_id=%s (status=%s) could not be reparsed as a "
                "signal — dropped: %r",
                channel_name, tg_id, _ex_status, text[:120],
            )
            return None

    _new_dir = _reparse["direction"].upper()
    _promote_execute = False
    if _new_dir == _ex_dir:
        if _reparse.get("entry_low") is not None:
            _log_reason = "backfill" if _ex_fields_null else "edit update"
            if _ex_status == "pending_followup":
                _log_reason = "pending_followup promoted"
            log.info(
                "[%s] Updating fields for tg_id=%s (%s)",
                channel_name, tg_id, _log_reason,
            )
            with db_module.db() as conn:
                conn.execute(
                    """UPDATE vantage_tg_signals
                       SET raw_text=?, entry_low=?, entry_high=?, stop_loss=?,
                           tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?, tp7=?, tp8=?,
                           status=CASE WHEN status='pending_followup' THEN 'new' ELSE status END
                       WHERE tg_message_id=?""",
                    (text,
                     _reparse["entry_low"], _reparse["entry_high"], _reparse["stop_loss"],
                     _reparse.get("tp1"), _reparse.get("tp2"), _reparse.get("tp3"),
                     _reparse.get("tp4"), _reparse.get("tp5"), _reparse.get("tp6"),
                     _reparse.get("tp7"), _reparse.get("tp8"),
                     tg_id),
                )
            if _ex_status == "pending_followup":
                log.info(
                    "[%s] pending_followup tg_id=%s now complete — executing",
                    channel_name, tg_id,
                )
                _promote_execute = True
            else:
                if _ex_status in ("instant_activated", "instant_pending",
                                  "activated", "new"):
                    await find_and_apply_instant_followup_fn(
                        channel_name, _new_dir, _reparse, tg_id,
                    )
        else:
            with db_module.db() as conn:
                conn.execute(
                    "UPDATE vantage_tg_signals SET raw_text=? WHERE tg_message_id=?",
                    (text, tg_id),
                )
        if not _promote_execute:
            return None
        return _reparse

    if _ex_status in ("new",):
        log.warning(
            "[%s] EDIT CORRECTION tg_id=%s: %s → %s (status=%s) — updating signal",
            channel_name, tg_id, _ex_dir, _new_dir, _ex_status,
        )
        with db_module.db() as conn:
            conn.execute(
                """UPDATE vantage_tg_signals
                   SET direction=?, entry_low=?, entry_high=?, stop_loss=?,
                       tp1=?, tp2=?, tp3=?, tp4=?, tp5=?, tp6=?, tp7=?, tp8=?,
                       raw_text=?
                   WHERE tg_message_id=?""",
                (
                    _reparse["direction"],
                    _reparse["entry_low"], _reparse["entry_high"],
                    _reparse["stop_loss"],
                    _reparse.get("tp1"), _reparse.get("tp2"), _reparse.get("tp3"),
                    _reparse.get("tp4"), _reparse.get("tp5"), _reparse.get("tp6"),
                    _reparse.get("tp7"), _reparse.get("tp8"),
                    text, tg_id,
                ),
            )
        _alert_txt = (
            f"SIGNAL CORRECTED via edit\n"
            f"Channel: {channel_name}\n"
            f"{_ex_dir} → {_new_dir}  "
            f"Entry {_reparse['entry_low']}-{_reparse['entry_high']}  "
            f"SL {_reparse['stop_loss']}\n"
            f"Pending signal updated. Not yet executed."
        )
        asyncio.create_task(
            telegram_alerts.send_message(_alert_txt, tg_id, "signal_edit_corrected")
        )
    else:
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_tg_signals SET raw_text=? WHERE tg_message_id=?",
                (text, tg_id),
            )
        log.warning(
            "[%s] EDIT WARNING tg_id=%s: direction changed %s → %s "
            "but signal status=%s — manual review required",
            channel_name, tg_id, _ex_dir, _new_dir, _ex_status,
        )
        _alert_txt = (
            f"SIGNAL EDIT WARNING\n"
            f"Channel: {channel_name}\n"
            f"Message was edited: {_ex_dir} → {_new_dir}\n"
            f"Signal status: {_ex_status} — could not auto-correct.\n"
            f"Review any open trades manually."
        )
        asyncio.create_task(
            telegram_alerts.send_message(_alert_txt, tg_id, "signal_edit_warning")
        )
    return None
