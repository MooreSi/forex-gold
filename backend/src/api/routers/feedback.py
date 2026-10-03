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


class FeedbackSent(BaseModel):
    ok: bool
    id: str


@router.post("", response_model=FeedbackSent)
async def send(body: FeedbackWrite) -> FeedbackSent:
    try:
        entry = feedback_ctl.submit(body.kind, body.message)
    except ValueError as exc:
        raise Refusal(str(exc), status_code=400) from exc
    return FeedbackSent(ok=True, id=entry["id"])
