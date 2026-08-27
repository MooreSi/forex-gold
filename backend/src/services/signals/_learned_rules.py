"""AI-derived parse rules, and the SL-adjustment rules that go with them.

Lifted out of parser.py for the 800-line ceiling. A self-contained concern: it
takes a channel's learned rule rows and tries them against a message before the
hand-written parsers get a look in.

Takes _autocorrect_tps from _tp_autocorrect rather than from parser, because
parser imports THIS module to re-export it -- reaching back would be a circular
import.
"""
from __future__ import annotations

import json
import re
from typing import Optional

from backend.src.services.signals._tp_autocorrect import _autocorrect_tps


_TP_NUMBER_RE = re.compile(r"\d+\.?\d*")
_RULE_PATTERN_MAX_LEN = 300


def _safe_compile_rule_pattern(pattern: str):
    if not pattern or len(pattern) > _RULE_PATTERN_MAX_LEN:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        return None

def apply_learned_rule(rule: dict, text: str) -> Optional[dict]:
    """Run one AI-derived rule (gate/direction/entry/sl/tp_block patterns)
    against text. Returns a parse_gold_signal()-shaped dict, or None if the
    rule doesn't match this text at all (a normal, expected outcome — most
    rules only apply to messages of the one shape they were derived from)."""
    gate = _safe_compile_rule_pattern(rule.get("gate_pattern", ""))
    direction_p = _safe_compile_rule_pattern(rule.get("direction_pattern", ""))
    entry_p = _safe_compile_rule_pattern(rule.get("entry_pattern", ""))
    sl_p = _safe_compile_rule_pattern(rule.get("sl_pattern", ""))
    tp_p = _safe_compile_rule_pattern(rule.get("tp_block_pattern", ""))
    if not all([gate, direction_p, entry_p, sl_p, tp_p]):
        return None
    if not gate.search(text):
        return None

    dm = direction_p.search(text)
    if not dm or dm.lastindex != 1:
        return None
    direction = dm.group(1).upper()
    if direction not in ("BUY", "SELL"):
        return None

    em = entry_p.search(text)
    if not em or em.lastindex != 2:
        return None
    try:
        e1, e2 = float(em.group(1)), float(em.group(2))
    except ValueError:
        return None

    slm = sl_p.search(text)
    if not slm or slm.lastindex != 1:
        return None
    try:
        sl_val = float(slm.group(1))
    except ValueError:
        return None

    tpm = tp_p.search(text)
    if not tpm or tpm.lastindex != 1:
        return None
    tps = [float(m) for m in _TP_NUMBER_RE.findall(tpm.group(1))][:8]
    if not tps:
        return None

    raw = {
        "direction": direction,
        "entry_low": min(e1, e2),
        "entry_high": max(e1, e2),
        "stop_loss": sl_val,
    }
    for i in range(1, 9):
        raw[f"tp{i}"] = tps[i - 1] if i <= len(tps) else None
    return _autocorrect_tps(direction, raw["entry_low"], raw["entry_high"], raw)

def parse_with_learned_rules(text: str, channel_name: str) -> Optional[dict]:
    """Try every AI-derived rule saved for this channel (most recent first)
    before the caller ever needs to fall back to a live AI call — the whole
    point of the approve-and-learn workflow. Returns None if no saved rule
    matches, in which case the caller proceeds exactly as before."""
    from backend.src.db import database as _db
    for rule_row in _db.get_learned_parser_rules(channel_name):
        try:
            rule = json.loads(rule_row.get("pattern") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        parsed = apply_learned_rule(rule, text)
        if parsed and parsed.get("tp1") is not None:
            return parsed
    return None

def apply_sl_adjustment_rule(rule: dict, text: str) -> Optional[float]:
    """Run one AI-derived SL-adjustment rule (gate_pattern, sl_value_pattern)
    against text — the sibling of apply_learned_rule() for follow-up "Adjust
    SL to X" style messages rather than new entry signals. Returns the new
    stop-loss value, or None if the rule doesn't match (expected for most
    messages — each rule only applies to the one channel/wording it was
    derived from)."""
    gate = _safe_compile_rule_pattern(rule.get("gate_pattern", ""))
    sl_p = _safe_compile_rule_pattern(rule.get("sl_value_pattern", ""))
    if not gate or not sl_p:
        return None
    if not gate.search(text):
        return None
    m = sl_p.search(text)
    if not m or m.lastindex != 1:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None

def check_sl_adjustment_rules(text: str, channel_name: str) -> Optional[float]:
    """Try every approved ai_derived_sl_adjust rule for this channel before
    the caller falls back to a live AI classification call — same
    approve-and-learn shortcut as parse_with_learned_rules(), for the
    SL-adjustment message category instead of new entries."""
    from backend.src.db import database as _db
    for rule_row in _db.get_learned_rules_by_type(channel_name, "ai_derived_sl_adjust"):
        try:
            rule = json.loads(rule_row.get("pattern") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        result = apply_sl_adjustment_rule(rule, text)
        if result is not None:
            return result
    return None
