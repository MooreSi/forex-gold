"""core_logic_keywords.py: editable phrase lexicons backing the Parsing
page's "Logic Keywords" section."""
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.services.telegram import keywords as lk


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    os.remove(path)


def test_get_lexicon_returns_default_when_unset(fresh_db):
    assert lk.get_lexicon("close_all") == lk.DEFAULT_LEXICONS["close_all"]


def test_get_lexicon_unknown_category_returns_empty(fresh_db):
    assert lk.get_lexicon("not_a_real_category") == []


def test_set_and_get_lexicon_round_trips(fresh_db):
    lk.set_lexicon("close_all", ["FLAT NOW", "OUT", "  ", "close now"])
    assert lk.get_lexicon("close_all") == ["FLAT NOW", "OUT", "CLOSE NOW"]


def test_set_lexicon_unknown_category_raises(fresh_db):
    with pytest.raises(ValueError):
        lk.set_lexicon("bogus", ["X"])


def test_set_lexicon_overwrite_replaces_not_merges(fresh_db):
    lk.set_lexicon("exclusion", ["A", "B"])
    lk.set_lexicon("exclusion", ["C"])
    assert lk.get_lexicon("exclusion") == ["C"]


def test_get_all_lexicons_covers_every_category(fresh_db):
    all_lex = lk.get_all_lexicons()
    assert set(all_lex.keys()) == set(lk.DEFAULT_LEXICONS.keys())


def test_set_all_lexicons_writes_each_category(fresh_db):
    lk.set_all_lexicons({"close_all": ["X"], "exclusion": ["Y"]})
    assert lk.get_lexicon("close_all") == ["X"]
    assert lk.get_lexicon("exclusion") == ["Y"]
    # Untouched categories keep their default
    assert lk.get_lexicon("risk_free_be") == lk.DEFAULT_LEXICONS["risk_free_be"]


def test_text_matches_any_case_insensitive_substring():
    assert lk.text_matches_any("please Close All now", ["CLOSE ALL"]) == "CLOSE ALL"


def test_text_matches_any_no_match_returns_none():
    assert lk.text_matches_any("random chatter", ["CLOSE ALL", "RISK FREE"]) is None


def test_text_matches_any_empty_inputs():
    assert lk.text_matches_any("", ["X"]) is None
    assert lk.text_matches_any("text", []) is None


def test_claim_trigger_first_time_true_second_time_false(fresh_db):
    assert lk.claim_trigger("tg1", "close_all") is True
    assert lk.claim_trigger("tg1", "close_all") is False


def test_claim_trigger_different_trigger_types_independent(fresh_db):
    assert lk.claim_trigger("tg1", "close_all") is True
    assert lk.claim_trigger("tg1", "tp_hit") is True
