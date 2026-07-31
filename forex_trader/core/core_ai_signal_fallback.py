"""AI signal fallback + SL-adjustment application + unrecognised-message
analysis -- extracted verbatim (no logic changes) from core/engine.py's
SimulationEngine._try_ai_signal_fallback/_push_ai_recovered_created/
_apply_sl_adjustment/_queue_unrecognised/_analyse_unrecognised_message, as
part of the core/engine.py migration series. See
docs/todo/refactor/core-ai-signal-fallback-migration/020-*.md.

Calls bridge.modify_order -- a real MT5 order-modify call, unchanged from
the original, only in apply_sl_adjustment. This module places, closes, or
modifies no order itself; it only calls whatever `bridge` its caller
supplies.

ai_signal_extractor.classify_message/claude_ai.classify_unknown_message are
real AI-provider calls, an already-extracted, stable module untouched by
this pack -- called through exactly as the original did.

`is_active_trader_node` is taken as an explicit bool (the caller's
already-computed answer from SimulationEngine._is_active_trader_node,
which belongs to a separate, not-yet-extracted cluster) rather than
recomputed here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

from backend.src.services.ai import signal_extractor as ai_signal_extractor
from backend.src.services.ai import claude_ai as claude_ai
from forex_trader.core import database as db_module
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.signals.parser import _CURRENCY_RE

log = logging.getLogger(__name__)


async def try_ai_signal_fallback(
    text: str, channel_name: str, tg_id: str, cfg: dict,
    is_active_trader_node: bool, bridge: Any,
) -> Optional[dict]:
    """Last-resort AI extraction for a message the deterministic
    parser (is_format_ab_signal/parse_gold_signal, is_gd2_message/
    parse_gd2_signal) failed to recognise or fully parse. Only called
    after the deterministic path has already given up — see
    ai_signal_extractor.py for why this is safe (confidence floor, no
    fabricated numbers on bare triggers, no action on chatter).

    Gated to the active-trader node only (see _is_active_trader_node) —
    the standby side of a paired Mac/VPS returns None immediately without
    recording a dedup check, so nothing is lost: if this node is later
    promoted, the message is still sitting unclassified in the reader's
    buffer and gets a normal first attempt then.

    The same AI call also classifies "Adjust SL to X"-style follow-up
    instructions (see ai_signal_extractor.classify_message) — handled
    entirely here (apply + queue for review) since it's not a new entry
    signal for the caller to execute, so this still returns None for
    that case exactly as it would for chatter.

    Deduplicated on (tg_id, text) via db_module.has_ai_fallback_check()/
    record_ai_fallback_check() — the Telegram reader's message buffer
    (get_buffer_messages) holds the last N messages regardless of
    processing status, and this function is reached on every scan cycle
    (~1s) for any message that still fails deterministic parsing. Without
    this, one piece of chatter that's neither a signal nor an
    SL-adjustment got reclassified by a live paid AI call every single
    cycle, forever (confirmed live 2026-07-08 via a temporary
    caller-debug patch in ai_provider.complete() — this was the entire
    explanation for API credits draining with no corresponding Telegram
    activity). The check only gets recorded *after* a successful AI call
    (any definitive result, including "not a signal") — a transient
    failure (network blip, timeout) is deliberately NOT recorded, so it's
    retried next cycle instead of giving up on a message permanently.
    Keyed on text too, not just tg_id, so a genuine edit (new text under
    the same tg_id) still gets classified fresh.

    A cheap pre-check reuses the existing "Currency:" regex so the AI
    never even gets asked about an explicitly-non-XAUUSD message (the
    AI's own prompt is gold-only and untested against other pairs)."""
    if not is_active_trader_node:
        return None

    if await db_module.to_db_thread(db_module.has_ai_fallback_check, tg_id, text):
        return None

    cm = _CURRENCY_RE.search(text)
    if cm and cm.group(1).upper().replace("/", "").replace("-", "") != "XAUUSD":
        return None
    try:
        result = await ai_signal_extractor.classify_message(text, channel_name, cfg)
    except Exception as e:
        log.debug("[AI-Fallback] tg_id=%s extraction call failed: %s", tg_id, e)
        return None
    await db_module.to_db_thread(db_module.record_ai_fallback_check, tg_id, text)
    if result is None:
        return None

    if result.get("kind") == "sl_adjustment":
        log.info(
            "[AI-Fallback] tg_id=%s %s recognised an SL-adjustment instruction "
            "(confidence=%.2f): new_sl=%s — %s",
            tg_id, channel_name, result.get("_ai_confidence", 0),
            result.get("new_stop_loss"), result.get("_ai_reasoning", ""),
        )
        await db_module.to_db_thread(
            db_module.save_ai_recovered_sl_adjustment,
            tg_id, channel_name, text, result["new_stop_loss"],
            result.get("_ai_confidence", 0), result.get("_ai_reasoning", ""),
        )
        await push_ai_recovered_created(
            tg_id, channel_name, text, "sl_adjustment",
            result.get("_ai_confidence", 0), result.get("_ai_reasoning", ""),
            new_stop_loss=result["new_stop_loss"],
        )
        await apply_sl_adjustment(
            result["new_stop_loss"], channel_name, tg_id, "ai_fallback", bridge,
        )
        return None  # not a new entry signal — nothing for the caller to execute

    log.info(
        "[AI-Fallback] tg_id=%s %s recovered a signal the deterministic parser missed "
        "(confidence=%.2f): %s",
        tg_id, channel_name, result.get("_ai_confidence", 0), result.get("_ai_reasoning", ""),
    )
    await db_module.to_db_thread(
        db_module.save_ai_recovered_signal,
        tg_id, channel_name, text, result,
        result.get("_ai_confidence", 0), result.get("_ai_reasoning", ""),
    )
    await push_ai_recovered_created(
        tg_id, channel_name, text, "signal",
        result.get("_ai_confidence", 0), result.get("_ai_reasoning", ""),
        **{k: result.get(k) for k in
           ("direction", "entry_low", "entry_high", "stop_loss",
            "tp1", "tp2", "tp3", "tp4", "tp5", "tp6", "tp7", "tp8")},
    )
    return result


async def push_ai_recovered_created(
    tg_id: str, channel_name: str, text: str, message_type: str,
    confidence: float, reasoning: str, **fields,
) -> None:
    """Mirror a newly-created ai_recovered_signals row to the paired
    Local/Remote node — see sync/protocol.py's MSG_AI_RECOVERED_SIGNAL_SYNC
    comment. Best-effort: a missed delivery is caught by the periodic
    full-queue pull (sync.client.SyncClient._ai_recovered_pull_loop)."""
    try:
        from forex_trader.sync import server as sync_server
        srv = sync_server.get_instance()
        if srv is not None:
            await srv.push_own_ai_recovered_signal(
                "created", tg_message_id=tg_id, channel_name=channel_name,
                raw_text=text, message_type=message_type,
                confidence=confidence, reasoning=reasoning, **fields,
            )
            return
    except Exception as e:
        log.debug("[AI-Fallback] peer sync via server skipped/failed: %s", e)
    try:
        from forex_trader.sync import client as sync_client
        from forex_trader.sync.protocol import CONN_CONNECTED
        cli = sync_client.get_instance()
        if cli.conn_state == CONN_CONNECTED:
            await cli.push_ai_recovered_signal(
                "created", tg_message_id=tg_id, channel_name=channel_name,
                raw_text=text, message_type=message_type,
                confidence=confidence, reasoning=reasoning, **fields,
            )
    except Exception as e:
        log.debug("[AI-Fallback] peer sync via client skipped/failed: %s", e)


async def apply_sl_adjustment(
    new_sl: float, channel_name: str, tg_id: str, via: str, bridge: Any,
) -> None:
    """Apply a recognised "Adjust SL to X" instruction to whichever open
    trade this channel most recently produced. via is "learned_rule"
    (fast, deterministic match — see signal_parser.check_sl_adjustment_rules)
    or "ai_fallback" (first time this channel's wording is seen; also
    queued for review in Telegram > Reader Logic > AI so a rule gets
    generated for next time).

    Safe no-op if this node has no matching open trade locally — e.g. it's
    the stood-down side of a Local/Remote pair and never executed the
    position in the first place (see open_trade()'s active_trader gate).
    Modifying an *already open* trade's SL, unlike opening a new one, is
    safe to attempt redundantly from either node since MT5 itself is the
    single source of truth for the real position — both nodes reaching
    the same target SL from the same message is harmless, not a race.

    Deduplicated on tg_id via db_module.try_claim_sl_adjustment() — the
    Telegram reader's message buffer (get_buffer_messages) is re-scanned
    every ~1s regardless of whether a message was already handled, and
    without this a matched learned rule kept re-firing on the same
    message every cycle for as long as it stayed buffered, each time
    sending a fresh "SL adjusted" alert (found live 2026-07-08 — same
    message re-triggered roughly once a minute for over half an hour)."""
    if not await db_module.to_db_thread(
        db_module.try_claim_sl_adjustment, tg_id, channel_name, new_sl,
    ):
        return

    with db_module.db() as conn:
        row = conn.execute(
            "SELECT * FROM vantage_simulated_trades WHERE status='open' AND "
            "(tg_source=? OR tg_source=? OR tg_source LIKE ?) "
            "ORDER BY open_time DESC LIMIT 1",
            (channel_name, f"instant:{channel_name}", f"Telegram Auto ({channel_name})"),
        ).fetchone()
    trade = db_module.row_to_dict(row) if row else None
    if not trade:
        log.info(
            "[%s] SL adjustment (tg_id=%s, via=%s) target %.2f — no matching "
            "open trade found locally, nothing to apply",
            channel_name, tg_id, via, new_sl,
        )
        return

    mt5_ticket = trade.get("mt5_ticket")
    trade_id   = trade["trade_id"]
    old_sl     = trade.get("stop_loss")
    if old_sl is not None and abs(float(old_sl) - new_sl) < 0.011:
        log.info(
            "[%s] SL adjustment (tg_id=%s, via=%s) target %.2f already matches "
            "trade=%s's current SL — no-op",
            channel_name, tg_id, via, new_sl, trade_id[:8],
        )
        return
    try:
        if mt5_ticket:
            await bridge.modify_order(int(mt5_ticket), sl=new_sl, tp=None)
        with db_module.db() as conn:
            conn.execute(
                "UPDATE vantage_simulated_trades SET stop_loss=? WHERE trade_id=?",
                (new_sl, trade_id),
            )
        log.info(
            "[%s] SL adjustment (tg_id=%s, via=%s) applied to trade=%s (ticket %s): %s -> %s",
            channel_name, tg_id, via, trade_id[:8], mt5_ticket, old_sl, new_sl,
        )
        _via_label = {
            "learned_rule": "learned rule",
            "ai_fallback": "AI-recovered instruction",
            "logic_keyword": "Logic Keywords (RISK FREE/BE lexicon)",
        }.get(via, via)
        _alert = (
            f"SL adjusted — {channel_name}\n"
            f"Trade {trade_id[:8]} (ticket {mt5_ticket}): {old_sl} → {new_sl}\n"
            f"Source: {_via_label}"
        )
        asyncio.create_task(
            telegram_alerts.send_message(_alert, tg_id, "sl_adjustment_applied")
        )
    except Exception as e:
        log.error(
            "[%s] SL adjustment (tg_id=%s) FAILED to apply to trade=%s (ticket %s): %s",
            channel_name, tg_id, trade_id[:8], mt5_ticket, e,
        )
        asyncio.create_task(telegram_alerts.send_message(
            f"SL ADJUSTMENT FAILED — {channel_name}\nTrade {trade_id[:8]} "
            f"(ticket {mt5_ticket}): tried to set SL to {new_sl}, error: {e}\nReview manually.",
            tg_id, "sl_adjustment_failed",
        ))


def queue_unrecognised(tg_id: str, channel_name: str, text: str, cfg: dict) -> None:
    try:
        with db_module.db() as conn:
            exists = conn.execute(
                "SELECT id FROM channel_unrecognised_messages WHERE tg_message_id=?",
                (tg_id,),
            ).fetchone()
        if not exists:
            unrec_id = db_module.save_unrecognised_message(channel_name, tg_id, text)
            if unrec_id:
                asyncio.create_task(
                    analyse_unrecognised_message(unrec_id, channel_name, text, cfg)
                )
                log.info("[%s] Unrecognised message tg_id=%s — queued for analysis", channel_name, tg_id)
    except Exception as e:
        log.warning("_queue_unrecognised failed: %s", e)


async def analyse_unrecognised_message(
    unrec_id: int, channel_name: str, text: str, cfg: dict,
) -> None:
    analysis: dict = {}
    try:
        analysis = await claude_ai.classify_unknown_message(
            text, channel_name, cfg
        )
        db_module.update_unrecognised_message(
            unrec_id, claude_analysis=__import__("json").dumps(analysis)
        )
    except Exception as e:
        log.warning("_analyse_unrecognised_message failed: %s", e)
        db_module.update_unrecognised_message(
            unrec_id,
            claude_analysis=__import__("json").dumps({"error": str(e), "summary": "Analysis failed"}),
        )
    try:
        summary = analysis.get("summary", "Unknown message") if analysis else "Unknown message"
        await telegram_alerts.send_message(
            f"Unrecognised message from *{channel_name}*\n"
            f"Analysis: {summary}\n"
            f"Open the Telegram tab to review and classify it.",
            event_type="unrecognised_message",
        )
    except Exception:
        pass
