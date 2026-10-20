"""The Analysis calendar's day journal and news history (docs/todo/023).
Words and past events for planning and review; nothing that trades reads them,
and no route here reaches the broker."""
from __future__ import annotations

from fastapi import APIRouter, Path, Query

from backend.src.api.errors import Refusal
from backend.src.api.schemas.calendar import (
    DAY_PATTERN, JournalIn, JournalList, JournalSaved, NewsList,
)
from backend.src.controllers import calendar_controller as calendar_ctl

router = APIRouter(prefix="/api/calendar", tags=["calendar"])

START = Query(..., pattern=DAY_PATTERN)
END = Query(..., pattern=DAY_PATTERN)


@router.get("/journal", response_model=JournalList)
async def journal(start: str = START, end: str = END) -> dict:
    return {"entries": await calendar_ctl.journal_between(start, end)}


@router.put("/journal/{day}", response_model=JournalSaved)
async def save_journal(body: JournalIn, day: str = Path(..., pattern=DAY_PATTERN)) -> dict:
    try:
        return {"entry": await calendar_ctl.save_journal(day, body.note, body.tags, body.mood)}
    except calendar_ctl.BadJournalEntry as exc:
        raise Refusal(str(exc), status_code=422) from None


@router.get("/news", response_model=NewsList)
async def news(start: str = START, end: str = END) -> dict:
    return {"events": await calendar_ctl.news_between(start, end)}
