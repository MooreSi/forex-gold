"""The Calendar's day journal (docs/todo/023, #8): a note, tags and a 1-5 mood
per trading day, for the account that is open.

Words about the trading only. Nothing that trades, signals or gates reads
them. The shape rules live here, not only at the API, because they are what
keeps one day's row small enough to read a month of on every calendar open.
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from backend.src.services.analytics import day_journal_repo as repo

MAX_NOTE = 4000
MAX_TAGS = 10
MAX_TAG_LEN = 32


class BadJournalEntry(ValueError):
    pass


def _account_env() -> str:
    """demo or live. A column rather than a separate database -- see
    day_journal_repo's header."""
    from backend.src import config as _config
    return str(_config.get("account_env", "demo"))


def _check_day(day: str) -> str:
    try:
        if len(day) != 10 or date.fromisoformat(day).isoformat() != day:
            raise ValueError
    except ValueError:
        raise BadJournalEntry(f"{day!r} is not a date (YYYY-MM-DD).") from None
    return day


def between(start: str, end: str) -> list[dict]:
    """Entries with start <= day <= end. Compared as text, so an `end` of
    YYYY-MM-31 covers any month without being a real date."""
    return repo.between(_account_env(), start, end)


def save(day: str, note: str, tags: list[str], mood: Optional[int]) -> Optional[dict]:
    """Store the day's entry, replacing any before it. A blank entry (no note,
    no tags, no mood) removes the day, so clearing a note leaves no marker.
    Returns the stored entry, or None when the day was cleared."""
    _check_day(day)
    note = (note or "").strip()
    if len(note) > MAX_NOTE:
        raise BadJournalEntry(f"A note is at most {MAX_NOTE} characters.")
    clean: list[str] = []
    for t in tags or []:
        t = str(t).strip()
        if len(t) > MAX_TAG_LEN:
            raise BadJournalEntry(f"A tag is at most {MAX_TAG_LEN} characters.")
        if t and t not in clean:
            clean.append(t)
    if len(clean) > MAX_TAGS:
        raise BadJournalEntry(f"At most {MAX_TAGS} tags a day.")
    if mood is not None and not 1 <= int(mood) <= 5:
        raise BadJournalEntry("Mood is 1 to 5.")
    env = _account_env()
    if not note and not clean and mood is None:
        repo.delete(env, day)
        return None
    return repo.upsert(env, day, note, clean, None if mood is None else int(mood))


async def between_async(start: str, end: str) -> list[dict]:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(between, start, end)


async def save_async(day: str, note: str, tags: list[str], mood: Optional[int]) -> Optional[dict]:
    from backend.src.db.database import to_db_thread
    return await to_db_thread(save, day, note, tags, mood)
