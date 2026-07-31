"""
LLM-based fallback signal extractor for the Telegram reader.

The deterministic parsers in signal_parser.py (is_format_ab_signal/
parse_gold_signal, is_gd2_message/parse_gd2_signal) are regex/keyword based
and silently miss a message the instant a channel tweaks its wording — an
added risk-level line, a relabelled SL field, etc. (confirmed live: a
"High Risk" prefix line alone broke is_gd2_message()'s gate entirely, so the
message never even reached parse attempt or the unrecognised-message queue).

extract_signal() is called only as a fallback, after the deterministic parser
has already failed, and only produces a usable result when the model is
confident AND the message contains real, complete price levels — a bare
trigger ("Sell Zone Now") or actual chatter never becomes a trade. See
[[project_telegram_ai_fallback]] memory for the verification this was tested
against before going live.
"""
import json
import logging
import re
from typing import Optional

from backend.src.services.ai import provider as ai_provider
from forex_trader.core.signal_parser import _autocorrect_tps

log = logging.getLogger(__name__)

_MIN_CONFIDENCE = 0.6

_SYSTEM = """You extract XAUUSD (gold) trading signals from Telegram messages for an automated trading app. These messages come from known channel styles, but wording, labels, and extra lines (risk warnings, disclaimers, emoji) vary and drift over time — you must handle novel phrasing, not just memorised examples.

Common style A: conversational, e.g. "Direction BUY / Currency: XAUUSD / ENTRY: 4053-4050 / TP1: 4055 ... TP8: 4081 / SL: 4047", often preceded by a disclaimer line.

Common style B: terse, e.g. "Buy Gold Now / 4056.5 - 4050.5 / Targets / 4058.5 / 4060.50 / 4063 / Open / SL/ invalid 4048" — may have an extra leading risk-level line ("High Risk", "Medium Risk", etc.) before the direction/action line; may label the stop line as "SL", "SL/invalid", "Stop Loss", or similar; may have "Targets" as a bare section header before a list of numbers instead of "TP1:"/"TP2:" labels; "Open" is a filler word, not a price.

A message may also be:
- A bare instant trigger with no numbers yet (e.g. "Sell Zone Now", "XAU USD BUY NOW") — a real signal intent, but with no extractable levels; a fuller message with the numbers typically follows within minutes.
- A follow-up trade-management instruction for an existing position rather than a new signal — most commonly an instruction to move the stop loss to a specific price, e.g. "Adjust SL to 4060", "Move SL to 4060", "New SL 4060", "SL now 4060". Recognise this even with varied wording, but ONLY when a specific numeric price is given — a vague instruction with no explicit number ("move to breakeven", "secure some profit") is NOT an SL adjustment, since there is no way to verify the correct price from the message text alone.
- Not a trading signal or management instruction at all (chat, commentary, an unrelated currency pair).

Respond with ONLY a single minified JSON object, no other text:
{"is_signal": bool, "is_instant_trigger_only": bool, "direction": "BUY"|"SELL"|null, "entry_low": number|null, "entry_high": number|null, "stop_loss": number|null, "tps": [number, ...], "is_sl_adjustment": bool, "new_stop_loss": number|null, "confidence": 0.0-1.0, "reasoning": "one sentence"}

entry_low/entry_high: if only one entry price is given, use it for both. tps: list the target prices in the order given, however many there are (0 to 8). If the message is not a signal, set is_signal false and leave numeric fields null/empty. If it's a bare instant trigger (direction implied, no price levels at all), set is_signal true, is_instant_trigger_only true, direction set, all price fields null/empty. is_sl_adjustment/new_stop_loss: set is_sl_adjustment true and new_stop_loss to the exact numeric price ONLY for a clear, specific SL-move instruction as described above — a message is_signal or is_sl_adjustment, never both. Only set confidence high (>0.7) when the relevant fields for whichever category applies are all unambiguous."""


async def _classify(text: str, channel_name: str, cfg: dict) -> Optional[dict]:
    """The one AI call shared by extract_signal() and extract_sl_adjustment()
    — returns the raw parsed JSON dict, or None if the call/parse failed."""
    if not ai_provider.is_configured(cfg):
        return None
    try:
        raw = await ai_provider.complete(
            cfg, _SYSTEM, f"Channel: {channel_name}\n\nMessage:\n{text[:1500]}",
            max_tokens=500, timeout=20,
        )
        m = re.search(r"\{.*\}", raw, re.DOTALL)
        if not m:
            return None
        return json.loads(m.group(0))
    except Exception as e:
        log.debug("[AI-Signal] extraction call failed: %s", e)
        return None


def _interpret_signal(data: dict) -> Optional[dict]:
    """Shared by classify_message()/extract_signal() — turns a raw
    classification dict into a parse_gold_signal()-shaped result, or None if
    it isn't a confidently-extractable complete new signal."""
    if not data.get("is_signal") or data.get("is_instant_trigger_only"):
        return None
    try:
        if float(data.get("confidence", 0) or 0) < _MIN_CONFIDENCE:
            return None
    except (TypeError, ValueError):
        return None

    direction = (data.get("direction") or "").upper()
    if direction not in ("BUY", "SELL"):
        return None

    try:
        entry_low  = data.get("entry_low")
        entry_high = data.get("entry_high")
        stop_loss  = data.get("stop_loss")
        if entry_low is None or entry_high is None or stop_loss is None:
            return None
        entry_low  = float(entry_low)
        entry_high = float(entry_high)
        stop_loss  = float(stop_loss)
        tps = [float(t) for t in (data.get("tps") or [])][:8]
    except (TypeError, ValueError):
        return None
    if not tps:
        return None

    parsed = {
        "direction":  direction,
        "entry_low":  min(entry_low, entry_high),
        "entry_high": max(entry_low, entry_high),
        "stop_loss":  stop_loss,
    }
    for i in range(1, 9):
        parsed[f"tp{i}"] = tps[i - 1] if i <= len(tps) else None

    parsed = _autocorrect_tps(direction, parsed["entry_low"], parsed["entry_high"], parsed)
    if parsed.get("tp1") is None:
        return None  # autocorrect dropped every TP (all on the wrong side of entry) — not usable

    parsed["_ai_extracted"] = True
    parsed["_ai_confidence"] = float(data.get("confidence", 0) or 0)
    parsed["_ai_reasoning"] = str(data.get("reasoning", ""))[:300]
    return parsed


def _interpret_sl_adjustment(data: dict) -> Optional[dict]:
    """Shared by classify_message() — turns a raw classification dict into
    an SL-adjustment result, or None if it isn't a confident, specific,
    numeric SL-move instruction."""
    if not data.get("is_sl_adjustment"):
        return None
    try:
        if float(data.get("confidence", 0) or 0) < _MIN_CONFIDENCE:
            return None
        new_sl = float(data.get("new_stop_loss"))
    except (TypeError, ValueError):
        return None
    return {
        "new_stop_loss": new_sl,
        "_ai_extracted": True,
        "_ai_confidence": float(data.get("confidence", 0) or 0),
        "_ai_reasoning": str(data.get("reasoning", ""))[:300],
    }


async def extract_signal(text: str, channel_name: str, cfg: dict) -> Optional[dict]:
    """Try to extract a tradeable XAUUSD signal from a message the
    deterministic parser couldn't handle.

    Returns a dict shaped exactly like parse_gold_signal()/parse_gd2_signal()
    (direction, entry_low, entry_high, stop_loss, tp1..tp8) plus
    _ai_extracted/_ai_confidence/_ai_reasoning for audit visibility, or None
    if the message isn't a confidently-extractable complete signal (chatter,
    a bare trigger with no levels yet, low confidence, malformed numbers, or
    the AI call itself failed).
    """
    data = await _classify(text, channel_name, cfg)
    if data is None:
        return None
    return _interpret_signal(data)


async def classify_message(text: str, channel_name: str, cfg: dict) -> Optional[dict]:
    """Single entry point for the engine's fallback — one AI call classifies
    the message as either a new entry signal or a follow-up SL-adjustment
    instruction (see [[project_telegram_ai_rule_learning]] for why these are
    kept in one call instead of two: most fallback-eligible messages are
    channel chatter, and a second unconditional AI call per miss would double
    that cost for no benefit).

    Returns the same dict extract_signal() would (plus "kind": "signal"), or
    {"kind": "sl_adjustment", "new_stop_loss": ..., "_ai_extracted": ...,
    "_ai_confidence": ..., "_ai_reasoning": ...}, or None if neither applies.
    """
    data = await _classify(text, channel_name, cfg)
    if data is None:
        return None

    sig = _interpret_signal(data)
    if sig is not None:
        sig["kind"] = "signal"
        return sig

    adj = _interpret_sl_adjustment(data)
    if adj is not None:
        adj["kind"] = "sl_adjustment"
        return adj

    return None
