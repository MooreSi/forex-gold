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

from backend.src.services.broker import ea_templates as ea_templates
from backend.src.db import database as db_module
from backend.src.services.telegram import repo as telegram_repo
from backend.src.services.telegram import alerts as telegram_alerts
from backend.src.services.telegram.keywords import claim_trigger, get_lexicon, text_matches_any
from backend.src.services.telegram import alerts
from backend.src.services.positions.core_pips import PIPS_TO_PRICE_XAUUSD

log = logging.getLogger(__name__)

def _find_channel_open_trade(channel_name: str) -> Optional[dict]:
    row = telegram_repo.find_channel_open_trade(channel_name)
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


# Last-resort stop distance for apply_sl_parsing_override, used when neither
# the channel's template nor the risk setting supplies a usable one. Never
# 0/None: every consumer past the parser (validate_signal, handle_limit_
# order_signal, suggest_lot_size, resolve_signal) types stop_loss as a real
# float, so a missing stop is not a state this app can represent.
_FALLBACK_SL_PIPS = 50.0


def apply_sl_parsing_override(parsed: dict, rs: dict, channel_name: str) -> Optional[str]:
    """Enable SL Parsing OFF (2026-08-05) -- replaces the signal's own stated
    Stop Loss with one derived from configuration, in place on `parsed`.
    Returns a short description of what changed (for the log), or None if the
    toggle is on and the signal keeps its own stop.

    This used to set parsed["stop_loss"] = None outright, on the reasoning
    that downstream consumers would then see the same "missing field" shape
    they already handle for a signal that never had one. They don't: the
    field is typed as a required float everywhere past the parser, so a None
    raised TypeError out of validate_signal (`None >= entry_low`) and
    handle_limit_order_signal (`float(None)`) instead. With the engine's
    scanner catch being per-cycle at the time, one such signal aborted the
    whole scan pass -- confirmed live 2026-08-05: seven aborted cycles in
    100 minutes, nothing executed at all while the toggle was off.

    Distance, first usable one wins:
      1. the channel's assigned EA Template `sl_pips` (a template is a
         self-contained per-channel definition and already outranks the
         signal's own stop in core_signal_resolution -- so it must win here
         too, or the toggle would hand this channel a different stop than
         the template it is configured with),
      2. Fallback SL Distance (Parsing page),
      3. _FALLBACK_SL_PIPS.

    Anchored to the far edge of the entry zone (BUY: entry_low, SELL:
    entry_high) so the result is below/above the whole zone by construction
    and passes validate_signal's own direction checks no matter where in
    the zone the fill lands."""
    if bool(rs.get("lk_enable_sl_parsing", 1)):
        return None
    direction = str(parsed.get("direction") or "").upper()
    entry_low, entry_high = parsed.get("entry_low"), parsed.get("entry_high")
    if direction not in ("BUY", "SELL") or entry_low is None or entry_high is None:
        # Can't derive a stop without a direction and a zone. Leaving the
        # signal's own stated stop in place disregards the toggle, but it is
        # a real float -- and nothing downstream survives the alternative.
        log.warning("[LogicKeywords] SL Parsing OFF but signal has no direction/entry zone "
                    "— keeping its stated stop (%s)", parsed.get("stop_loss"))
        return None

    pips = 0.0
    src = ""
    _ch_ov = db_module.get_channel_strategy_override(channel_name)
    if ea_templates.is_template_override(_ch_ov):
        _tpl = ea_templates.get_ea_template(ea_templates.template_name_from_override(_ch_ov))
        if _tpl:
            pips = float(_tpl.get("sl_pips") or 0)
            src = f"template '{_tpl['name']}' sl_pips"
    if pips <= 0:
        pips = float(rs.get("lk_fallback_sl_pips", _FALLBACK_SL_PIPS) or 0)
        src = "Fallback SL Distance"
    if pips <= 0:
        pips = _FALLBACK_SL_PIPS
        src = "built-in default"

    dist  = pips * PIPS_TO_PRICE_XAUUSD
    stated = parsed.get("stop_loss")
    parsed["stop_loss"] = round(
        float(entry_low) - dist if direction == "BUY" else float(entry_high) + dist, 2,
    )
    return (f"SL Parsing OFF — stated stop {stated} replaced with "
            f"{parsed['stop_loss']:.2f} ({pips:.0f} pips from zone, via {src})")


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
    from backend.src.services.trading.ai_signal_fallback import apply_sl_adjustment
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
