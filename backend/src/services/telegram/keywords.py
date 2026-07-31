"""Logic Keywords -- editable phrase lexicons backing the Parsing page's
"Logic Keywords" section (2026-07-22). Six categories, each a flat list of
phrases matched case-insensitively as a plain substring anywhere in a
message's text:

  symbol_tokens  -- gates whether the message is even considered for new-
                    signal parsing at all (see core_logic_keyword_triggers.
                    should_skip_for_symbol_or_exclusion).
  exclusion      -- messages containing any of these are ignored outright
                    before parsing (noise: alerts, results, education posts).
  close_all      -- closes whichever open trade the triggering channel most
                    recently produced (see core_logic_keyword_triggers.
                    try_handle_close_all_trigger).
  risk_free_be   -- moves that same trade's SL to its own entry price
                    (breakeven) -- delegates to core_ai_signal_fallback.
                    apply_sl_adjustment for the actual move/dedup/alert.
  buy_orders     -- reference list only (not itself matched against
                    anything) -- the words this app's own direction-
                    detection regexes already respond to across its
                    several channel formats, shown here so they're
                    editable/extendable rather than buried in regex.
  limit_orders   -- same, for the pending-order ("BUY LIMITS GOLD @ x/y
                    AREA") layout specifically.

buy_orders/limit_orders are informational/extensible reference lists, not
literal regex replacements -- this app's actual direction/limit-order
detection stays in signal_parser.py's per-format regexes (Format A/B, GD2,
GD2-limits, instant-entry); editing these boxes does not rewrite those
regexes. They exist here as an editable, visible summary of what those
regexes currently key off, and a place for a future generic keyword-hint
layer to read from.
"""
from __future__ import annotations

import json
import logging
import time
from typing import Optional

from backend.src.db import database as db_module

log = logging.getLogger(__name__)

DEFAULT_LEXICONS: dict[str, list[str]] = {
    "symbol_tokens": ["GOLD", "XAUUSD", "XAU"],
    "close_all": [
        "CLOSE ALL", "CLOSE NOW", "CLOSE ENTRIES", "CLOSE THIS", "CLOSE TRADE",
        "I AM OUT", "OUT THIS TRADE", "EXIT NOW", "EXIT TRADE", "EXIT AT",
        "CLOSED OUT", "CLOSE AT BE", "OUT AT BREAKEVEN", "CANCEL SETUP",
        "INVALIDATED", "DELETE LIMITS", "DELETE PENDING",
    ],
    "risk_free_be": [
        "RISK FREE", "RISK-FREE", "SECURE RUNNERS", "MOVE TO BE",
        "MOVE TO BREAKEVEN", "SET BE", "BREAK EVEN", "BREAKEVEN",
        "BRING SL TO ENTRY", "SL TO ENTRY", "SL ENTRY", "SECURE ENTRY",
        "LOCK IN", "LOCK ENTRY", "LOCK PROFITS", "LOCKING PROFITS",
    ],
    "exclusion": ["ALERT", "RESULT", "EDUCATION", "SUMMARY"],
    # Extracted from signal_parser.py's own direction-detection regexes
    # (_DIRECTION_RE, _DIRECTION_B_RE, _GD2_DIRECTION_RE, _GD2_ZONE_DIRECTION_RE,
    # parse_instant_entry) -- see module docstring above.
    "buy_orders": [
        "BUY", "BUY NOW", "BUY GOLD", "BUY ZONE", "BUY ZONE NOW",
        "BUY GOLD NOW", "XAU USD BUY", "XAUUSD BUY", "DIRECTION BUY",
    ],
    # Extracted from _GD2_LIMITS_DIRECTION_RE / is_limit_order_signal.
    "limit_orders": [
        "LIMIT", "LIMITS", "AREA", "BUY LIMITS GOLD", "SELL LIMITS GOLD",
        "BUY GOLD @", "SELL GOLD @",
    ],
}

LEXICON_LABELS: dict[str, str] = {
    "symbol_tokens": "Target Asset/Symbol Tokens",
    "close_all": "CLOSE ALL Trigger Lexicon",
    "risk_free_be": "RISK FREE / BE Trigger Lexicon",
    "exclusion": "Exclusion Keywords / Filter",
    "buy_orders": "BUY Orders",
    "limit_orders": "LIMIT Orders",
}

LEXICON_HELP: dict[str, str] = {
    "symbol_tokens": "Comma-separated list of symbols triggering copier parsing. E.g. GOLD, XAUUSD, XAU",
    "close_all": "Comma-separated exit command phrases. E.g. CLOSE ALL, CLOSE NOW, CLOSE TRADE",
    "risk_free_be": "Comma-separated breakeven safety phrases. E.g. RISK FREE, SET BE, MOVE TO BE",
    "exclusion": "Messages containing these keywords will be ignored. E.g. ALERT, RESULT, EDUCATION",
    "buy_orders": "Comma-separated phrases this app's own parsers already treat as a BUY signal.",
    "limit_orders": "Comma-separated phrases this app's own parsers already treat as a pending LIMIT order.",
}

_CATEGORIES = tuple(DEFAULT_LEXICONS.keys())


def default_lexicon(category: str) -> list[str]:
    return list(DEFAULT_LEXICONS.get(category, []))


def get_lexicon(category: str) -> list[str]:
    """Live phrase list for `category` -- DB override if saved, else the
    built-in default. Never raises on a corrupt/missing row."""
    with db_module.db() as conn:
        row = conn.execute(
            "SELECT phrases_json FROM logic_keyword_lexicons WHERE category=?",
            (category,),
        ).fetchone()
    if not row:
        return default_lexicon(category)
    try:
        phrases = json.loads(row[0])
        return [str(p) for p in phrases if str(p).strip()]
    except Exception as exc:
        log.warning("[LogicKeywords] bad phrases_json for %s: %s", category, exc)
        return default_lexicon(category)


def get_all_lexicons() -> dict[str, list[str]]:
    return {cat: get_lexicon(cat) for cat in _CATEGORIES}


def set_lexicon(category: str, phrases: list[str]) -> list[str]:
    if category not in DEFAULT_LEXICONS:
        raise ValueError(f"Unknown Logic Keywords category: {category}")
    clean = [p.strip().upper() for p in phrases if p and p.strip()]
    with db_module.db() as conn:
        conn.execute(
            "INSERT INTO logic_keyword_lexicons (category, phrases_json) VALUES (?,?) "
            "ON CONFLICT(category) DO UPDATE SET phrases_json=excluded.phrases_json",
            (category, json.dumps(clean)),
        )
    return clean


def set_all_lexicons(lexicons: dict[str, list[str]]) -> None:
    for category, phrases in lexicons.items():
        if category in DEFAULT_LEXICONS:
            set_lexicon(category, phrases)


def text_matches_any(text: str, phrases: list[str]) -> Optional[str]:
    """Returns the first matching phrase (case-insensitive substring), or
    None if nothing in `phrases` appears in `text`."""
    if not text or not phrases:
        return None
    upper = text.upper()
    for phrase in phrases:
        if phrase and phrase.upper() in upper:
            return phrase
    return None


def claim_trigger(tg_message_id: str, trigger_type: str) -> bool:
    """Dedup guard for the CLOSE ALL / RISK FREE-BE / TP HIT triggers --
    same shape as core_db_ai_recovered.try_claim_sl_adjustment. Returns True
    (and records the claim) the first time this (tg_message_id, trigger_type)
    pair is seen, False on every subsequent call against the same buffered
    message."""
    try:
        with db_module.db() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO logic_keyword_triggers_applied "
                "(tg_message_id, trigger_type, applied_at) VALUES (?,?,?)",
                (tg_message_id, trigger_type, time.time()),
            )
            return cur.rowcount > 0
    except Exception as exc:
        log.warning("[LogicKeywords] claim_trigger failed: %s", exc)
        return False
