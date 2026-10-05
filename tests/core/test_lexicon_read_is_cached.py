"""The keyword lexicons are read from the database once per few seconds, not
on every message (bugs/030, after the React port).

The Mac's stall watchdog named `get_lexicon_json` as the top of the stack in
44 event-loop stalls over a second: the signal scanner asks for a lexicon
several times per message, on the loop, and each read can wait behind a
writer. The phrases only change when someone saves them (set_lexicon, which
also receives the peer's copy in channel-setup sync).

Same shape as strategy_params' cache: a short TTL, cleared on every write
and on a database switch.
"""
from backend.src.services.telegram import keywords
from backend.src.services.telegram import repo as telegram_repo


def _count_reads(monkeypatch):
    calls = []
    real = telegram_repo.get_lexicon_json
    monkeypatch.setattr(telegram_repo, "get_lexicon_json",
                        lambda cat: calls.append(cat) or real(cat))
    return calls


def test_repeated_reads_hit_the_database_once(fresh_db, monkeypatch):
    calls = _count_reads(monkeypatch)
    for _ in range(5):
        keywords.get_lexicon("exclusion")
    assert len(calls) == 1


def test_a_save_is_seen_at_once(fresh_db, monkeypatch):
    keywords.get_lexicon("exclusion")
    keywords.set_lexicon("exclusion", ["hold on"])
    assert keywords.get_lexicon("exclusion") == ["HOLD ON"]


def test_a_database_switch_is_seen_at_once(fresh_db, tmp_path):
    keywords.set_lexicon("exclusion", ["first db"])
    assert keywords.get_lexicon("exclusion") == ["FIRST DB"]
    from backend.src.db import database as db_module
    db_module.init(str(tmp_path / "other.db"))
    assert keywords.get_lexicon("exclusion") == keywords.default_lexicon("exclusion")


def test_the_cache_expires(fresh_db, monkeypatch):
    calls = _count_reads(monkeypatch)
    keywords.get_lexicon("exclusion")
    monkeypatch.setattr(keywords.time, "monotonic", lambda: 1e12)
    keywords.get_lexicon("exclusion")
    assert len(calls) == 2
