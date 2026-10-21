"""Shapes for the Analysis calendar's journal and news (docs/todo/023)."""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

# Shape only. Whether it is a real date is the service's question: a range
# end of YYYY-MM-31 is deliberate (see day_journal.between).
DAY_PATTERN = r"^\d{4}-\d{2}-\d{2}$"


class JournalIn(BaseModel):
    note: str = Field("", max_length=4000)
    tags: list[str] = Field(default_factory=list, max_length=10)
    mood: Optional[int] = Field(None, ge=1, le=5)


class JournalEntryOut(BaseModel):
    day: str
    note: str
    tags: list[str]
    mood: Optional[int]
    updated_at: float


class JournalList(BaseModel):
    entries: list[JournalEntryOut]


class JournalSaved(BaseModel):
    """The stored entry, or null when saving a blank day cleared it."""
    entry: Optional[JournalEntryOut]


class NewsEventOut(BaseModel):
    day: str
    ts: float
    title: str
    currency: str
    impact: str


class NewsList(BaseModel):
    events: list[NewsEventOut]


class ChartNewsEventOut(BaseModel):
    ts: float
    title: str
    currency: str
    impact: str
    forecast: str = ""
    previous: str = ""


class ChartNewsList(BaseModel):
    events: list[ChartNewsEventOut]
