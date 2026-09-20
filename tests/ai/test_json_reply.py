"""Reading a JSON object out of a model's reply.

Six places in this codebase did the same four lines: `if raw.startswith("```")`,
split, drop a leading `json`, split again, `json.loads`. That handles exactly
one of the ways a model wraps its answer, and every caller wraps the result in
a bare `except` that falls back silently.

The running app showed the cost on 2026-09-20:

    channel_strategy_ai: AI call failed: Expecting property name enclosed in
    double quotes: line 1 column 2 (char 1) -- using backtested baseline

Column 2 of line 1 means the reply began `{` followed by something that is not
a quote. The fallback worked, so nothing broke -- the AI strategy picker simply
never ran, on every cycle, and the only trace was a warning that did not
include the text that failed to parse. **That is the part this module fixes
first**: a failure here names what came back, so the next one can be diagnosed
instead of guessed at.

Nothing here calls a model. These are strings.
"""
from __future__ import annotations

import pytest

from backend.src.services.ai import json_reply


# ── The shapes a model actually returns ──────────────────────────────────────

def test_plain_json():
    assert json_reply.parse_json_object('{"strategy": "scale_out"}') == {
        "strategy": "scale_out"}


def test_a_fenced_block():
    raw = '```json\n{"strategy": "scale_out"}\n```'

    assert json_reply.parse_json_object(raw) == {"strategy": "scale_out"}


def test_a_fence_with_no_language_tag():
    raw = '```\n{"strategy": "scale_out"}\n```'

    assert json_reply.parse_json_object(raw) == {"strategy": "scale_out"}


def test_a_fence_tagged_with_something_else():
    """Models label the block whatever they feel like. The old four lines
    dropped exactly the four characters "json" and nothing else, so
    ```JSON or ```javascript left a language tag inside the payload."""
    raw = '```JSON\n{"strategy": "scale_out"}\n```'

    assert json_reply.parse_json_object(raw) == {"strategy": "scale_out"}


def test_prose_before_the_object():
    """"Here is the analysis you asked for:" is not JSON, and is the single
    most common thing a chat model puts in front of one."""
    raw = 'Here is my recommendation:\n{"strategy": "scale_out"}'

    assert json_reply.parse_json_object(raw) == {"strategy": "scale_out"}


def test_prose_after_the_object():
    raw = '{"strategy": "scale_out"}\n\nLet me know if you want more detail.'

    assert json_reply.parse_json_object(raw) == {"strategy": "scale_out"}


def test_leading_and_trailing_whitespace():
    assert json_reply.parse_json_object('\n\n  {"a": 1}  \n') == {"a": 1}


class TestPythonFlavouredReplies:
    """The failure seen on the running app. Some models answer with a Python
    dict repr rather than JSON -- single quotes, and True/False/None."""

    def test_single_quotes(self):
        assert json_reply.parse_json_object("{'strategy': 'scale_out'}") == {
            "strategy": "scale_out"}

    def test_python_booleans_and_none(self):
        out = json_reply.parse_json_object("{'skip': True, 'note': None}")

        assert out == {"skip": True, "note": None}

    def test_it_is_literals_only(self):
        """`ast.literal_eval`, never `eval`: this is untrusted text from a
        remote service, and the tolerant path must not become a way to run
        something."""
        with pytest.raises(ValueError):
            json_reply.parse_json_object("{'x': __import__('os').getcwd()}")


# ── Nested objects survive ───────────────────────────────────────────────────

def test_a_nested_object_is_not_truncated_at_the_first_brace():
    raw = 'Result:\n{"a": {"b": 2}, "c": 3}\ndone'

    assert json_reply.parse_json_object(raw) == {"a": {"b": 2}, "c": 3}


def test_a_per_channel_map_round_trips():
    raw = ('{"Gold Diggers VIP": {"strategy": "scale_out", "confidence": 0.8},'
           ' "GD2": {"strategy": "be_runner", "confidence": 0.6}}')

    out = json_reply.parse_json_object(raw)

    assert out["GD2"]["strategy"] == "be_runner"


# ── Failures say what came back ──────────────────────────────────────────────

class TestAFailureIsDiagnosable:

    def test_it_raises_rather_than_returning_an_empty_dict(self):
        """An empty dict would be indistinguishable from "the model had no
        recommendations", and every caller's fallback is written for a
        raised error."""
        with pytest.raises(ValueError):
            json_reply.parse_json_object("I cannot help with that request.")

    def test_the_message_quotes_the_reply(self):
        with pytest.raises(ValueError, match="I cannot help"):
            json_reply.parse_json_object("I cannot help with that request.")

    def test_a_long_reply_is_trimmed_in_the_message(self):
        """A model can return a page of text. The log line must stay a log
        line."""
        with pytest.raises(ValueError) as exc:
            json_reply.parse_json_object("x" * 5000)

        assert len(str(exc.value)) < 700

    def test_an_empty_reply_says_so(self):
        with pytest.raises(ValueError, match="empty"):
            json_reply.parse_json_object("")

    def test_none_says_so(self):
        with pytest.raises(ValueError, match="empty"):
            json_reply.parse_json_object(None)

    def test_a_json_array_is_refused_by_name(self):
        """Callers index the result by key. A list would fail later, at a
        line that has nothing to do with the cause."""
        with pytest.raises(ValueError, match="list"):
            json_reply.parse_json_object('["scale_out"]')

    def test_the_context_is_included_when_given(self):
        with pytest.raises(ValueError, match="channel_strategy_ai"):
            json_reply.parse_json_object("nonsense", context="channel_strategy_ai")
