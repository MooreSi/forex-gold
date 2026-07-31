"""Logic Keywords trigger handlers -- the Parsing page's "Logic Keywords"
section (2026-07-22) wires these into engine.py's _scan_messages, ahead of
the normal signal-classification pipeline. Each trigger is a plain,
global, user-editable phrase list (core_logic_keywords.py) rather than the
existing per-channel AI-learned-rule system (signal_parser.
check_sl_adjustment_rules) -- the two coexist; this is an additional,
simpler, always-on layer.

Design decisions confirmed with the user (2026-07-22):
  - CLOSE ALL closes only the triggering channel's own most-recently-opened
    trade, not every open trade system-wide.
  - TP HIT is log/notify only -- it never moves SL or closes anything on
    its own (that's what the separate RISK FREE/BE trigger is for, even
    though real channel messages often combine both in one line).

Real-money surface: try_handle_close_all_trigger calls close_trade_fn (a
genuine MT5 close) and try_handle_risk_free_be_trigger delegates to
core_ai_signal_fallback.apply_sl_adjustment (a genuine MT5 SL modify) --
both only when their lexicon phrase actually matches and the corresponding
Logic Keywords toggle is on.
"""
from __future__ import annotations

import asyncio
import logging
import re
from typing import Any, Awaitable, Callable, Optional

from forex_trader.core import core_ea_templates as ea_templates
from forex_trader.core import database as db_module
from forex_trader.core import telegram_alerts
from forex_trader.core.core_logic_keywords import claim_trigger, get_lexicon, text_matches_any

log = logging.getLogger(__name__)

# Same tg_source matching convention as core_ai_signal_fallback.apply_sl_adjustment
# -- direct channel name, an instant-entry-prefixed variant, or the
# "Telegram Auto (...)" wrapper auto-execution stamps trades with.
_OPEN_TRADE_SQL = (
    "SELECT * FROM vantage_simulated_trades WHERE status='open' AND "
    "(tg_source=? OR tg_source=? OR tg_source LIKE ?) "
    "ORDER BY open_time DESC LIMIT 1"
)


def _find_channel_open_trade(channel_name: str) -> Optional[dict]:
    with db_module.db() as conn:
        row = conn.execute(
            _OPEN_TRADE_SQL,
            (channel_name, f"instant:{channel_name}", f"Telegram Auto ({channel_name})"),
        ).fetchone()
    return db_module.row_to_dict(row) if row else None


def _tg_cmd_blocked(trade: dict) -> bool:
    """True if `trade` is managed by an EA Template whose TG CMD toggle is
    OFF -- Logic Keywords triggers (CLOSE ALL / RISK FREE-BE) must not act
    on it. Every non-template trade is unaffected (never blocked here)."""
    strategy = trade.get("strategy") or ""
    if not ea_templates.is_template_override(strategy):
        return False
    tpl = ea_templates.get_ea_template(ea_templates.template_name_from_override(strategy))
    return bool(tpl) and not tpl["tg_cmd_enabled"]


def should_skip_media_or_forwarded(msg: dict, rs: dict) -> Optional[str]:
    """Returns a skip reason if this message should be dropped before any
    other processing, else None."""
    if bool(rs.get("lk_ignore_media_messages", 1)) and bool(msg.get("has_media")):
        return "media message (Logic Keywords: Ignore Media Messages)"
    if bool(rs.get("lk_ignore_forwarded_messages", 0)) and bool(msg.get("forwarded")):
        return "forwarded message (Logic Keywords: Ignore Forwarded Messages)"
    return None


def should_skip_for_exclusion(text: str, rs: dict) -> Optional[str]:
    """Gates only the new-signal-parsing path (classify_and_parse) -- not
    the CLOSE ALL/RISK FREE-BE/TP HIT triggers above, which are short
    instruction messages that legitimately may not mention the symbol at
    all. Returns a skip reason, or None to proceed to parsing.

    Deliberately does NOT also gate on symbol_tokens ("only parse messages
    that mention GOLD/XAUUSD/XAU"), even though the reference tool this
    was modelled on does -- confirmed live against this app's own test
    suite (test_scan_messages_parse_classify_characterization.py) that
    several already-working message shapes never literally say the symbol
    name (e.g. a Format B entry with no separate "Currency:" line, or
    exactly the kind of odd wording the AI fallback exists to catch in the
    first place) and would have been silently dropped before ever reaching
    the parser or the AI fallback. symbol_tokens stays available as an
    editable reference list (Logic Keywords UI) but isn't wired to a block
    here."""
    exclusion = get_lexicon("exclusion")
    hit = text_matches_any(text, exclusion)
    if hit:
        return f"exclusion keyword matched (Logic Keywords): {hit}"
    return None


def should_skip_ai_fallback_for_no_signal_candidate(text: str, rs: dict) -> Optional[str]:
    """Gates only the AI-fallback last-resort recovery path (every
    ai_fallback_fn call site inside core_scan_messages_parse_classify.
    classify_and_parse) -- never the deterministic per-format parsers, which
    have their own correct format-specific gates and must keep working even
    for messages that don't literally name the symbol (see
    should_skip_for_exclusion's docstring above for a documented example:
    Gold Diggers 2.0's "Buy/Sell Zone Now" layout never says GOLD/XAU/XAUUSD
    anywhere -- gating deterministic parsing on symbol_tokens would silently
    drop that entire live channel format).

    2026-07-24: previously symbol_tokens/buy_orders/limit_orders were pure
    reference lists, never consulted by anything -- editing them in the
    Parsing UI had zero effect on live behaviour. This combines all three
    into one "does this look at all like it could be a trading signal"
    pre-filter ahead of spending an AI call, so they become a real, active
    part of parsing instead of cosmetic. Applying it only to the AI-fallback
    path (rather than blocking parsing outright) means every currently-
    working deterministic format keeps working exactly as before -- the gate
    only ever prunes the residual "nothing recognised this at all" case,
    which previously burned an AI call unconditionally on pure noise too.

    An empty combined lexicon (all three boxes cleared in the UI) disables
    the gate entirely rather than blocking every AI-fallback call forever --
    an accidental empty save must never silently kill the app's last-resort
    signal-recovery path."""
    phrases = get_lexicon("symbol_tokens") + get_lexicon("buy_orders") + get_lexicon("limit_orders")
    if not phrases:
        return None
    if text_matches_any(text, phrases):
        return None
    return "no symbol/buy/limit keyword matched (Logic Keywords) — AI fallback skipped"


def apply_mirror_copy(parsed: dict, rs: dict) -> Optional[str]:
    """Reverse/Mirror Copy (2026-07-31) -- inverts BUY<->SELL and reflects
    SL and every TP through the entry zone's midpoint, in place on `parsed`.
    Returns a short description of what changed (for the log/alert), or None
    if the toggle is off or the signal can't be mirrored.

    The midpoint is the pivot rather than a single entry price because every
    parser here produces an entry *range*: reflecting through the midpoint
    maps entry_low onto entry_high, so the zone lands exactly on itself and
    only the levels around it flip sides. That in turn keeps the mirrored
    SL/TP geometry valid by construction -- a BUY with its SL below the zone
    becomes a SELL with its SL above it -- so the mirrored dict passes the
    same validate_signal checks the original would have.

    Deliberately does NOT touch `tp_open`: that key is what routes a signal
    to the Limit Runner rather than market execution, and this app has no
    LIMIT/STOP order-type duality for a mirror to swap (confirmed with the
    user 2026-07-31). Mirroring changes which way the trade faces, not how
    the order is placed."""
    if not bool(rs.get("lk_enable_mirror_copy", 0)):
        return None
    direction = str(parsed.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        return None
    entry_low, entry_high = parsed.get("entry_low"), parsed.get("entry_high")
    if entry_low is None or entry_high is None:
        return None

    pivot = (float(entry_low) + float(entry_high)) / 2.0

    def _reflect(value):
        return None if value is None else round(2.0 * pivot - float(value), 5)

    parsed["direction"] = "SELL" if direction == "BUY" else "BUY"
    parsed["stop_loss"] = _reflect(parsed.get("stop_loss"))
    for i in range(1, 9):
        key = f"tp{i}"
        if key in parsed:
            parsed[key] = _reflect(parsed[key])
    return f"{direction} -> {parsed['direction']} mirrored through {pivot:.2f}"


async def try_handle_close_all_trigger(
    text: str, channel_name: str, tg_id: str, rs: dict,
    close_trade_fn: Callable[[str, str], Awaitable[dict]],
) -> bool:
    """Returns True if this message was a CLOSE ALL trigger (handled here,
    caller should move to the next message), False if it wasn't."""
    if not bool(rs.get("lk_enable_close_all_parsing", 1)):
        return False
    phrase = text_matches_any(text, get_lexicon("close_all"))
    if not phrase:
        return False
    if not await db_module.to_db_thread(claim_trigger, tg_id, "close_all"):
        return True
    trade = await db_module.to_db_thread(_find_channel_open_trade, channel_name)
    if not trade:
        log.info("[LogicKeywords] CLOSE ALL trigger (%s) matched '%s' -- no open trade for this channel",
                 channel_name, phrase)
        return True
    if await db_module.to_db_thread(_tg_cmd_blocked, trade):
        log.info("[LogicKeywords] CLOSE ALL trigger (%s) matched '%s' -- trade=%s's template has "
                 "TG CMD off, not acting", channel_name, phrase, trade["trade_id"][:8])
        return True
    trade_id = trade["trade_id"]
    try:
        await close_trade_fn(trade_id, "logic_keyword_close_all")
        log.info("[LogicKeywords] CLOSE ALL trigger (%s) matched '%s' -- closed trade=%s",
                 channel_name, phrase, trade_id[:8])
        asyncio.create_task(telegram_alerts.send_message(
            f"*CLOSE ALL trigger* — {channel_name}\nMatched phrase: \"{phrase}\"\n"
            f"Trade {trade_id[:8]} closed.",
            tg_id, "logic_keyword_close_all",
        ))
    except Exception as exc:
        log.warning("[LogicKeywords] CLOSE ALL trigger failed to close trade=%s: %s", trade_id[:8], exc)
    return True


async def try_handle_risk_free_be_trigger(
    text: str, channel_name: str, tg_id: str, rs: dict, bridge: Any,
) -> bool:
    """Returns True if this message was a RISK FREE/BE trigger (handled
    here), False if it wasn't. Delegates the actual SL move to
    core_ai_signal_fallback.apply_sl_adjustment -- same dedup, MT5 modify,
    and Telegram alert path the existing per-channel learned-rule system
    already uses, just with a fixed global phrase list picking the target
    (the trade's own entry price) instead of a numeric value parsed out of
    the message."""
    if not bool(rs.get("lk_enable_risk_free_be_parsing", 1)):
        return False
    phrase = text_matches_any(text, get_lexicon("risk_free_be"))
    if not phrase:
        return False
    # Own dedup claim (independent of apply_sl_adjustment's own, which only
    # ever runs below once a trade is actually found) -- without this, a
    # message matching the lexicon but with no open trade at the time (the
    # common case: most RISK FREE/BE chatter isn't tied to a trade this app
    # itself has open) re-logged every scan cycle for as long as it stayed
    # buffered, and worse, would misfire against a DIFFERENT trade that
    # opens later on the same channel while the old message is still
    # buffered -- confirmed live 2026-07-23 (rapid repeat "no open trade"
    # log lines for the same message across consecutive ~1s cycles).
    if not await db_module.to_db_thread(claim_trigger, tg_id, "risk_free_be"):
        return True
    trade = await db_module.to_db_thread(_find_channel_open_trade, channel_name)
    if not trade:
        log.info("[LogicKeywords] RISK FREE/BE trigger (%s) matched '%s' -- no open trade for this channel",
                 channel_name, phrase)
        return True
    if await db_module.to_db_thread(_tg_cmd_blocked, trade):
        log.info("[LogicKeywords] RISK FREE/BE trigger (%s) matched '%s' -- trade=%s's template has "
                 "TG CMD off, not acting", channel_name, phrase, trade["trade_id"][:8])
        return True
    entry_price = float(trade["entry_price"])
    from forex_trader.core.core_ai_signal_fallback import apply_sl_adjustment
    await apply_sl_adjustment(entry_price, channel_name, tg_id, "logic_keyword", bridge)
    return True


_TP_HIT_RE = re.compile(r'\bTP\s*\d+\b[^A-Za-z]{0,20}\bHIT', re.IGNORECASE)


async def try_handle_tp_hit_trigger(text: str, channel_name: str, tg_id: str, rs: dict) -> bool:
    """Returns True if this message reported a TP hit (handled here --
    log/notify only, confirmed with the user 2026-07-22: never moves SL or
    closes anything by itself)."""
    if not bool(rs.get("lk_enable_tp_hit_parsing", 1)):
        return False
    if not _TP_HIT_RE.search(text):
        return False
    if not await db_module.to_db_thread(claim_trigger, tg_id, "tp_hit"):
        return True
    log.info("[LogicKeywords] TP HIT reported by %s: %s", channel_name, text[:120])
    asyncio.create_task(telegram_alerts.send_message(
        f"*TP hit reported* — {channel_name}\n{text[:300]}",
        tg_id, "logic_keyword_tp_hit",
    ))
    return True
