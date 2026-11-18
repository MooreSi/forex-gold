"""The Feedback popup: feature requests, bug reports and general feedback.

Delivery to the owner (Telegram, email, admin console) happens behind the
controller, over the remote connection; this only accepts the text. Nothing
here places or closes an order.
"""
from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel

from backend.src.api.errors import Refusal
from backend.src.controllers import feedback_controller as feedback_ctl

router = APIRouter(prefix="/api/feedback", tags=["feedback"])


class FeedbackWrite(BaseModel):
    kind: str
    message: str
    #: Optional, so the owner can reply directly. Older dashboards omit it.
    email: str = ""


class FeedbackSent(BaseModel):
    ok: bool
    id: str


@router.post("", response_model=FeedbackSent)
async def send(body: FeedbackWrite) -> FeedbackSent:
    try:
        # The email only when given, so the call an older dashboard makes is
        # exactly the call it always made.
        entry = feedback_ctl.submit(body.kind, body.message,
                                    **({"email": body.email} if body.email else {}))
    except ValueError as exc:
        raise Refusal(str(exc), status_code=400) from exc
    return FeedbackSent(ok=True, id=entry["id"])


class Ok(BaseModel):
    ok: bool


@router.post("/notices/{entry_id}/seen", response_model=Ok)
async def seen(entry_id: str) -> Ok:
    """The sender closed "Feedback noted and completed". The notices themselves
    ride the header poll (`/api/system/header`, `feedback_done`)."""
    if not feedback_ctl.mark_seen(entry_id):
        raise Refusal("No such notice.", status_code=404)
    return Ok(ok=True)


class InboxRow(BaseModel):
    id: str
    kind: str
    message: str
    name: str = ""
    email: str = ""
    hostname: str = ""
    version: str = ""
    submitted_at: float = 0


class ReadWrite(BaseModel):
    ids: list[str]


class ReadDone(BaseModel):
    ok: bool
    marked: int


@router.get("/inbox", response_model=list[InboxRow])
async def inbox() -> list[dict]:
    """New feedback behind the admin node's title-bar alert. Empty on any
    other install. The count rides the header poll (`feedback_unread`)."""
    return feedback_ctl.unread_feedback()


@router.post("/inbox/read", response_model=ReadDone)
async def read(body: ReadWrite) -> ReadDone:
    """The owner opened the alert: mark the ids it showed as read."""
    return ReadDone(ok=True, marked=feedback_ctl.mark_read(body.ids))
