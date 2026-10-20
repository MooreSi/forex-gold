"""The Analysis calendar's journal and news (docs/todo/023). Forwards to
backend.src.services.analytics.day_journal and .calendar_news unchanged.
Nothing here trades."""
from __future__ import annotations

from backend.src.services.analytics import calendar_news as _news
from backend.src.services.analytics import day_journal as _journal

__all__ = ["journal_between", "save_journal", "news_between", "BadJournalEntry"]

BadJournalEntry = _journal.BadJournalEntry


async def journal_between(start: str, end: str) -> list[dict]:
    return await _journal.between_async(start, end)


async def save_journal(day: str, note: str, tags: list[str], mood: int | None) -> dict | None:
    return await _journal.save_async(day, note, tags, mood)


async def news_between(start: str, end: str) -> list[dict]:
    return await _news.between_async(start, end)
