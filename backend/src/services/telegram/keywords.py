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
  buy_orders     -- phrases that name a BUY with no levels of their own
  sell_orders    -- the same, for a SELL
  limit_orders   -- reference list for the pending-order ("BUY LIMITS GOLD
                    @ x/y AREA") layout, matched only by the AI-fallback
                    gate below.

buy_orders/sell_orders are **live direction triggers** (2026-08-27). A
message with a line that IS one of these phrases, and no numbers anywhere,
is a bare direction heads-up: it takes the same path as the built-in bare
triggers ("XAU USD BUY", "Buy Zone Now") -- entered at market when
Immediate Market Entry is on for that channel, otherwise held quietly until
the message carrying the levels arrives.

They were reference lists until then: nothing matched them as a trigger, so
a phrase typed into the box bought nothing but permission to spend an AI
call. Reported live -- GOLD DIGGERS INSTITUTIONAL sent "PREPARE FOR A BUY"
with that exact phrase saved in the box and the app did nothing.

**Matching is per-line and exact, not substring**, unlike every other
lexicon here. The shipped default list contains the bare word "BUY"; as a
substring that would open a market order on any message that mentions
buying at all ("we are watching for a buy setup later"). Line-exact means
"BUY" fires on a message whose line is BUY and on nothing else. The
no-numbers rule is the second half of the same guard: anything stating a
level is a signal, or a fragment of one, and belongs to signal_parser.py's
per-format regexes rather than to a market order that would ignore what it
said.

Full-signal and limit-order detection is unchanged and still lives entirely
in those regexes -- these boxes do not rewrite them.
"""
from __future__ import annotations

import json
import logging
import re
import time
from typing import Optional

from backend.src.db import database as db_module
from backend.src.services.telegram import repo as telegram_repo

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
    # Seeded from signal_parser.py's own direction-detection regexes
    # (_DIRECTION_RE, _DIRECTION_B_RE, _GD2_DIRECTION_RE, _GD2_ZONE_DIRECTION_RE,
    # parse_instant_entry). Live triggers -- see the module docstring for why
    # the bare "BUY"/"SELL" entries are safe here and would not be as
    # substrings.
    "buy_orders": [
        "BUY", "BUY NOW", "BUY GOLD", "BUY ZONE", "BUY ZONE NOW",
        "BUY GOLD NOW", "XAU USD BUY", "XAUUSD BUY", "DIRECTION BUY",
    ],
    "sell_orders": [
        "SELL", "SELL NOW", "SELL GOLD", "SELL ZONE", "SELL ZONE NOW",
        "SELL GOLD NOW", "XAU USD SELL", "XAUUSD SELL", "DIRECTION SELL",
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
    "sell_orders": "SELL Orders",
    "limit_orders": "LIMIT Orders",
}

LEXICON_HELP: dict[str, str] = {
    "symbol_tokens": "Comma-separated list of symbols triggering copier parsing. E.g. GOLD, XAUUSD, XAU",
    "close_all": "Comma-separated exit command phrases. E.g. CLOSE ALL, CLOSE NOW, CLOSE TRADE",
    "risk_free_be": "Comma-separated breakeven safety phrases. E.g. RISK FREE, SET BE, MOVE TO BE",
    "exclusion": "Messages containing these keywords will be ignored. E.g. ALERT, RESULT, EDUCATION",
    "buy_orders": "Comma-separated phrases that mean BUY on their own, with no levels. A message whose line is one of these, and that contains no numbers, is entered at market when Immediate Market Entry is on for that channel.",
    "sell_orders": "The same, for SELL.",
    "limit_orders": "Comma-separated phrases this app's own parsers already treat as a pending LIMIT order.",
}

_CATEGORIES = tuple(DEFAULT_LEXICONS.keys())


def default_lexicon(category: str) -> list[str]:
    return list(DEFAULT_LEXICONS.get(category, []))


def get_lexicon(category: str) -> list[str]:
    """Live phrase list for `category` -- DB override if saved, else the
    built-in default. Never raises on a corrupt/missing row."""
    row = telegram_repo.get_lexicon_json(category)
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
    telegram_repo.upsert_lexicon(category, json.dumps(clean))
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


_DECORATION_RE = re.compile(r"[^A-Z0-9 ]+")
_HAS_DIGIT_RE = re.compile(r"\d")


def _normalise_line(line: str) -> str:
    """A line reduced to the words it actually says: markdown, emoji and
    punctuation stripped, whitespace collapsed, upper-cased. "**🔥 Prepare
    for a Buy! 🔥**" and "PREPARE FOR A BUY" normalise to the same string."""
    return " ".join(_DECORATION_RE.sub(" ", (line or "").upper()).split())


def text_line_matches_any(text: str, phrases: list[str]) -> Optional[str]:
    """The first phrase that some LINE of `text` is, normalised -- or None.

    Deliberately not text_matches_any's substring test. See the module
    docstring: these phrases become market orders, and "BUY" as a substring
    matches most of what a gold channel says all day.
    """
    if not text or not phrases:
        return None
    lines = {_normalise_line(l) for l in text.splitlines()}
    lines.discard("")
    if not lines:
        return None
    for phrase in phrases:
        norm = _normalise_line(phrase)
        if norm and norm in lines:
            return phrase
    return None


def text_has_number(text: str) -> bool:
    """True if `text` states any digit at all -- so it is a signal or part of
    one, not a bare direction heads-up."""
    return bool(_HAS_DIGIT_RE.search(text or ""))


def claim_trigger(tg_message_id: str, trigger_type: str) -> bool:
    """Dedup guard for the CLOSE ALL / RISK FREE-BE / TP HIT triggers --
    same shape as core_db_ai_recovered.try_claim_sl_adjustment. Returns True
    (and records the claim) the first time this (tg_message_id, trigger_type)
    pair is seen, False on every subsequent call against the same buffered
    message."""
    try:
        return telegram_repo.try_claim_trigger(tg_message_id, trigger_type, time.time())
    except Exception as exc:
        log.warning("[LogicKeywords] claim_trigger failed: %s", exc)
        return False
