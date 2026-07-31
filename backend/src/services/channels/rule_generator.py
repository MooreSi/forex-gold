"""
Automatic deterministic-rule generation from an approved AI-recovered signal.

Per the user's explicit choice: approving an AI-extracted message in
Telegram > Reader Logic > AI should be "fully automatic" — the app writes
and loads a new parsing rule itself, no human review of the generated
pattern. That's only safe because nothing here ever executes AI-generated
code: the model only ever produces regex PATTERN STRINGS (data, stored as
JSON), and a rule is never saved unless it passes a self-consistency check
— re-running the generated patterns against the *original* message and
confirming they reproduce the exact values a human already approved.

Rule shape (stored as JSON in channel_learned_rules.pattern, rule_type=
'ai_derived_parser'):
    {"gate_pattern": "...", "direction_pattern": "...",
     "entry_pattern": "...", "sl_pattern": "...", "tp_block_pattern": "..."}

The AI only ever locates substrings/labels; the actual number parsing inside
a matched TP block is done by a fixed, already-tested regex in this file
(_NUMBER_RE), not by AI-generated logic — this keeps the model's job narrow
(and its patterns easy to validate) rather than asking it to reinvent
numeric parsing per channel.
"""
import json
import logging
import re
from typing import Optional

from backend.src.services.ai import provider as ai_provider
from backend.src.db import database as db_module
from backend.src.services.signals.parser import apply_learned_rule

log = logging.getLogger(__name__)

_TOLERANCE = 0.011  # float compare tolerance for the self-consistency check

_SYSTEM = """You write Python regular expressions (re module syntax) that deterministically locate parts of a specific trading-signal message format, given one confirmed example. You are NOT writing extraction logic yourself — you only need to locate substrings; a fixed, separate number-parser reads values out of what you locate.

You will be given the raw message text and the already-confirmed-correct extracted values (direction, entry_low, entry_high, stop_loss, tp1..tp8).

Produce a JSON object with these regex pattern strings (Python re syntax, case-insensitive matching will be applied automatically — do not include inline flags):
{
  "gate_pattern": "a pattern that matches ONLY IF the text is this specific message format — base it on structural wording (e.g. a distinctive phrase or label), NEVER on the specific numbers in this example, since future messages will have different prices",
  "direction_pattern": "a pattern with exactly ONE capture group that captures the direction word (buy/sell, case-insensitive) — must generalise, not hardcode BUY or SELL literally with no group",
  "entry_pattern": "a pattern with exactly TWO capture groups capturing the two entry-zone numbers (order does not matter, do not hardcode the literal numbers from the example — capture number patterns like [\\\\d.]+ instead)",
  "sl_pattern": "a pattern with exactly ONE capture group capturing the stop-loss number (again, capture a number pattern, not the literal example value)",
  "tp_block_pattern": "a pattern with exactly ONE capture group capturing the ENTIRE substring that contains all the target-price numbers in order (whether they are individually labelled like 'TP1 ...TP2...' or just listed sequentially after a header like 'Targets') — capture the whole block, do not try to extract individual numbers yourself"
}

CRITICAL: every numeric capture group must match a general number pattern (e.g. [\\d.]+), never the literal digits from this one example — the whole point is these patterns must work on FUTURE messages with different prices. Respond with ONLY the JSON object, no other text."""


def _close(a: Optional[float], b: Optional[float]) -> bool:
    if a is None or b is None:
        return False
    try:
        return abs(float(a) - float(b)) < _TOLERANCE
    except (TypeError, ValueError):
        return False


def _validate_rule(rule: dict, raw_text: str, parsed: dict) -> tuple[bool, str]:
    """Re-run the generated rule through the exact same code live parsing
    will use (apply_learned_rule) and confirm it reproduces the values a
    human already approved. Returns (ok, reason) — reason explains the
    failure when ok is False."""
    result = apply_learned_rule(rule, raw_text)
    if result is None:
        return False, "rule did not match the source message at all (bad pattern or gate)"

    if result.get("direction") != (parsed.get("direction") or "").upper():
        return False, f"extracted direction {result.get('direction')!r}, expected {parsed.get('direction')!r}"

    if not (_close(result.get("entry_low"), parsed.get("entry_low")) and
            _close(result.get("entry_high"), parsed.get("entry_high"))):
        return False, (
            f"extracted entry {result.get('entry_low')}-{result.get('entry_high')}, "
            f"expected {parsed.get('entry_low')}-{parsed.get('entry_high')}"
        )

    if not _close(result.get("stop_loss"), parsed.get("stop_loss")):
        return False, f"extracted SL {result.get('stop_loss')}, expected {parsed.get('stop_loss')}"

    # Every TP slot must match exactly — both that each confirmed TP
    # reappears (a partial match means the TP block boundary is wrong and
    # would silently truncate future signals' TP ladders) AND that no *extra*
    # TP gets fabricated beyond what was confirmed. The latter is a real
    # failure mode: an over-captured tp_block_pattern can sweep up an
    # unrelated number (e.g. the SL value) as a bogus extra TP, which
    # _autocorrect_tps() then "fixes" by extrapolating a replacement —
    # passing a naive check that only compares up to the first confirmed
    # None, while silently inventing a TP the human never approved.
    for i in range(1, 9):
        expected = parsed.get(f"tp{i}")
        got = result.get(f"tp{i}")
        if expected is None:
            if got is not None:
                return False, f"TP{i} was fabricated as {got} — not part of the confirmed extraction"
            continue
        if not _close(got, expected):
            return False, f"TP{i} extracted as {got}, expected {expected}"

    return True, "validated: reproduces the confirmed direction/entry/SL/TPs exactly"


async def generate_rule(
    raw_text: str, channel_name: str, parsed: dict, tg_message_id: str, cfg: dict,
) -> tuple[Optional[int], str]:
    """Ask the AI to derive a regex-based rule from one approved example,
    validate it reproduces the confirmed extraction, and save it if so.
    Returns (rule_id, note) — rule_id is None if generation or validation
    failed (note explains why); the original AI-extracted signal is
    unaffected either way, since it already executed before this ever runs.
    """
    if not ai_provider.is_configured(cfg):
        return None, "AI provider not configured"

    prompt = (
        f"Channel: {channel_name}\n\n"
        f"Message:\n{raw_text[:1500]}\n\n"
        f"Confirmed extraction: direction={parsed.get('direction')} "
        f"entry_low={parsed.get('entry_low')} entry_high={parsed.get('entry_high')} "
        f"stop_loss={parsed.get('stop_loss')} "
        f"tps={[parsed.get(f'tp{i}') for i in range(1, 9) if parsed.get(f'tp{i}') is not None]}"
    )

    # The AI doesn't always nail every pattern on the first attempt (regex
    # generation isn't deterministic) — a wrong attempt is harmless since
    # _validate_rule() rejects it before it's ever saved, so retrying a few
    # times meaningfully improves the real success rate at negligible cost.
    _MAX_ATTEMPTS = 3
    reason = "no attempts made"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = await ai_provider.complete(cfg, _SYSTEM, prompt, max_tokens=600, timeout=30)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                reason = "AI response contained no JSON object"
                continue
            rule = json.loads(m.group(0))
        except Exception as e:
            log.warning("[AI-Rule] generation call failed (attempt %d): %s: %s",
                        attempt, type(e).__name__, e)
            reason = f"generation call failed: {type(e).__name__}: {e}"
            continue

        ok, reason = _validate_rule(rule, raw_text, parsed)
        if ok:
            break
        log.info("[AI-Rule] %s rule rejected (attempt %d/%d): %s",
                 channel_name, attempt, _MAX_ATTEMPTS, reason)
    else:
        return None, f"failed after {_MAX_ATTEMPTS} attempts — last reason: {reason}"

    rule_json = json.dumps(rule)
    notes = f"Auto-generated and self-validated from an approved AI extraction (tg_id={tg_message_id})."
    rule_id = db_module.save_channel_learned_rule(
        channel_name, "ai_derived_parser", rule_json, "auto_parse", notes, tg_message_id,
    )
    log.info("[AI-Rule] %s new deterministic rule saved (id=%s): %s", channel_name, rule_id, reason)
    await _broadcast_to_peer_node(
        channel_name, "ai_derived_parser", rule_json, "auto_parse", notes, tg_message_id,
    )
    return rule_id, reason


_SL_ADJUST_SYSTEM = """You write Python regular expressions (re module syntax) that deterministically recognise a specific "adjust the stop loss" trade-management message format, given one confirmed example. You are NOT writing extraction logic yourself — a fixed, separate parser reads the number out of what you locate.

You will be given the raw message text and the confirmed new stop-loss value it instructs.

Produce a JSON object with these regex pattern strings (Python re syntax, case-insensitive matching applied automatically — do not include inline flags):
{
  "gate_pattern": "a pattern that matches ONLY IF the text is this specific SL-adjustment wording — base it on structural phrasing (e.g. 'adjust sl', 'move sl', 'new sl'), NEVER on the specific number in this example, since future messages will have different prices",
  "sl_value_pattern": "a pattern with exactly ONE capture group capturing the new stop-loss number — capture a general number pattern like [\\\\d.]+, never hardcode the literal example value"
}

CRITICAL: the numeric capture group must match a general number pattern, never the literal digits from this one example. Respond with ONLY the JSON object, no other text."""


def _validate_sl_adjustment_rule(rule: dict, raw_text: str, new_stop_loss: float) -> tuple[bool, str]:
    """Mirrors _validate_rule() for the SL-adjustment rule shape: re-run the
    generated rule through the exact same code live parsing will use
    (apply_sl_adjustment_rule) and confirm it reproduces the confirmed
    value."""
    from backend.src.services.signals.parser import apply_sl_adjustment_rule
    result = apply_sl_adjustment_rule(rule, raw_text)
    if result is None:
        return False, "rule did not match the source message at all (bad pattern or gate)"
    if not _close(result, new_stop_loss):
        return False, f"extracted new_stop_loss {result}, expected {new_stop_loss}"
    return True, "validated: reproduces the confirmed new stop-loss exactly"


async def generate_sl_adjustment_rule(
    raw_text: str, channel_name: str, new_stop_loss: float, tg_message_id: str, cfg: dict,
) -> tuple[Optional[int], str]:
    """Sibling of generate_rule() for the SL-adjustment message category —
    same fully-automatic, self-validated design (see module docstring), just
    a narrower two-pattern rule shape since there's only one value to find."""
    if not ai_provider.is_configured(cfg):
        return None, "AI provider not configured"

    prompt = (
        f"Channel: {channel_name}\n\n"
        f"Message:\n{raw_text[:1500]}\n\n"
        f"Confirmed new stop-loss: {new_stop_loss}"
    )

    _MAX_ATTEMPTS = 3
    reason = "no attempts made"
    for attempt in range(1, _MAX_ATTEMPTS + 1):
        try:
            raw = await ai_provider.complete(cfg, _SL_ADJUST_SYSTEM, prompt, max_tokens=400, timeout=30)
            m = re.search(r"\{.*\}", raw, re.DOTALL)
            if not m:
                reason = "AI response contained no JSON object"
                continue
            rule = json.loads(m.group(0))
        except Exception as e:
            log.warning("[AI-Rule] SL-adjustment generation call failed (attempt %d): %s: %s",
                        attempt, type(e).__name__, e)
            reason = f"generation call failed: {type(e).__name__}: {e}"
            continue

        ok, reason = _validate_sl_adjustment_rule(rule, raw_text, new_stop_loss)
        if ok:
            break
        log.info("[AI-Rule] %s SL-adjustment rule rejected (attempt %d/%d): %s",
                 channel_name, attempt, _MAX_ATTEMPTS, reason)
    else:
        return None, f"failed after {_MAX_ATTEMPTS} attempts — last reason: {reason}"

    rule_json = json.dumps(rule)
    notes = f"Auto-generated and self-validated from an approved SL-adjustment instruction (tg_id={tg_message_id})."
    rule_id = db_module.save_channel_learned_rule(
        channel_name, "ai_derived_sl_adjust", rule_json, "auto_apply_sl", notes, tg_message_id,
    )
    log.info("[AI-Rule] %s new SL-adjustment rule saved (id=%s): %s", channel_name, rule_id, reason)
    await _broadcast_to_peer_node(
        channel_name, "ai_derived_sl_adjust", rule_json, "auto_apply_sl", notes, tg_message_id,
    )
    return rule_id, reason


async def _broadcast_to_peer_node(
    channel_name: str, rule_type: str, rule_json: str, action: str, notes: str, tg_message_id: str,
) -> None:
    """Mirror a newly-saved rule to the paired Local/Remote node — each node
    runs its own independent Telethon session and parses every message
    against its own local channel_learned_rules, so without this, approving
    a signal on one node would only teach that node the new format; the
    other would keep needing its own separate AI fallback call (and its own
    separate approval) for the same message shape. Best-effort: a missed
    delivery just means the peer falls back to learning it independently."""
    try:
        from backend.src.controllers.sync import server as sync_server
        srv = sync_server.get_instance()
        if srv is not None:
            await srv.push_own_learned_rule(
                channel_name, rule_type, rule_json, action, notes, tg_message_id,
            )
            return
    except Exception as e:
        log.debug("[AI-Rule] peer sync via server skipped/failed: %s", e)

    try:
        from backend.src.controllers.sync import client as sync_client
        from backend.src.controllers.sync.protocol import CONN_CONNECTED
        cli = sync_client.get_instance()
        if cli.conn_state == CONN_CONNECTED:
            await cli.push_learned_rule(
                channel_name, rule_type, rule_json, action, notes, tg_message_id,
            )
    except Exception as e:
        log.debug("[AI-Rule] peer sync via client skipped/failed: %s", e)
