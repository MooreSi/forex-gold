"""Read a JSON object out of a model's reply.

Six call sites repeated the same four lines -- strip a ``` fence, drop a
leading `json`, strip the closing fence, `json.loads` -- and every one of them
wrapped the result in a bare `except` that fell back silently. That handles one
of the ways a model wraps an answer and hides the rest.

The running app showed the cost on 2026-09-20:

    channel_strategy_ai: AI call failed: Expecting property name enclosed in
    double quotes: line 1 column 2 (char 1) -- using backtested baseline

Column 2 of line 1: the reply began `{` and the next character was not a
quote. The fallback did its job, so nothing looked broken -- the AI strategy
picker simply never ran, on every cycle, and the warning did not include the
text that failed, so there was nothing to diagnose from.

**The first job of this module is that the failure names the reply.** The
tolerance is second, and deliberately narrow:

  * a fence with any language tag, or none;
  * prose before or after the object, which is the most common wrapper of all;
  * a Python dict repr -- single quotes, `True`/`False`/`None` -- which is what
    some models return when asked for JSON.

The Python path is `ast.literal_eval`, never `eval`. This is untrusted text
from a remote service and the tolerant path must not become a way to run
something.
"""
from __future__ import annotations

import ast
import json
import re
from typing import Optional

__all__ = ["parse_json_object"]

# How much of a bad reply to quote. Enough to see what the model did, short
# enough that the warning stays one log line.
_SNIPPET = 400

_FENCE = re.compile(r"^```[A-Za-z0-9_+-]*\s*\n?(.*?)\n?```\s*$", re.DOTALL)


def _strip_fence(text: str) -> str:
    match = _FENCE.match(text.strip())
    return match.group(1).strip() if match else text.strip()


def _outermost_object(text: str) -> Optional[str]:
    """The substring from the first `{` to the brace that closes it.

    Brace-matched rather than `text[first:last+1]`, so trailing prose that
    happens to contain a `}` cannot extend the slice, and quoted braces
    inside string values do not end it early.
    """
    start = text.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    quote = ""
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == quote:
                in_string = False
            continue
        if ch in "\"'":
            in_string = True
            quote = ch
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


# Distinguishes "nothing parsed" from a reply that legitimately parsed to
# None, which `ast.literal_eval` will happily produce from the text "None".
_UNPARSED = object()


def _first_that_parses(candidates: list):
    """The first candidate either loader accepts, or `_UNPARSED`.

    `json.loads` first so well-formed JSON keeps JSON's semantics, then
    `ast.literal_eval` for the Python dict reprs some models return.
    """
    for candidate in candidates:
        for load in (json.loads, ast.literal_eval):
            try:
                return load(candidate)
            except (ValueError, SyntaxError, TypeError, MemoryError,
                    RecursionError):
                continue
    return _UNPARSED


def _fail(reason: str, raw: str, context: str) -> ValueError:
    where = f"{context}: " if context else ""
    snippet = raw[:_SNIPPET] + ("..." if len(raw) > _SNIPPET else "")
    return ValueError(f"{where}{reason}. The model replied: {snippet!r}")


def parse_json_object(raw: Optional[str], context: str = "") -> dict:
    """The JSON object in `raw`, or a ValueError that quotes what came back.

    `context` names the caller and is put in front of the message, so a
    warning in the log says which AI call produced it.
    """
    if raw is None or not str(raw).strip():
        raise _fail("the reply was empty", "" if raw is None else str(raw), context)

    text = _strip_fence(str(raw))

    # The whole reply first, then just the braced object. Both are needed:
    # a bare object parses as-is, and one with prose on EITHER side only
    # parses once the braces are matched out of it.
    candidates = [text]
    braced = _outermost_object(text)
    if braced and braced != text:
        candidates.append(braced)

    parsed = _first_that_parses(candidates)
    if parsed is _UNPARSED:
        raise _fail("the reply is not JSON", str(raw), context)

    if isinstance(parsed, list):
        raise _fail("the reply is a JSON list, not an object", str(raw), context)
    if not isinstance(parsed, dict):
        raise _fail(f"the reply is a {type(parsed).__name__}, not an object",
                    str(raw), context)
    return parsed
