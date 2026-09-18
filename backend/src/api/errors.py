"""One error shape for the whole API, and one rule about what reaches the user.

The money rule from the frontend conventions is the reason this module is not
just `raise HTTPException`:

    Surface a rejection verbatim. If the backend refuses an order, the user
    needs the real reason, not "something went wrong".

So there are two kinds of failure and they are treated differently:

* `Refusal` — the backend considered the request and said no. A trading halt,
  a disconnected bridge, an out-of-hours window, a broker rejection. The text
  IS the answer and it goes to the client unchanged.
* anything else — an unexpected exception. The client gets a generic message
  and a correlation id; the detail goes to the log. An AttributeError's text
  is not information the user can act on, and it leaks internals.

Both produce the same JSON body, so the client has one thing to parse:

    {"error": {"kind": "refusal"|"internal", "message": str, "ref": str|null}}
"""
from __future__ import annotations

import logging
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

log = logging.getLogger(__name__)


class Refusal(Exception):
    """The backend said no, and the reason is meant for the user."""

    def __init__(self, message: str, status_code: int = 409):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def _body(kind: str, message: str, ref: str | None = None) -> dict:
    return {"error": {"kind": kind, "message": message, "ref": ref}}


def install(app: FastAPI) -> None:
    @app.exception_handler(Refusal)
    async def _refusal(request: Request, exc: Refusal):      # noqa: ANN001
        # Deliberately logged at info, not error: a refused order is the system
        # working. Logging it as an error trains people to ignore errors.
        log.info("[api] refused %s: %s", request.url.path, exc.message)
        return JSONResponse(status_code=exc.status_code,
                            content=_body("refusal", exc.message))

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, exc: Exception):  # noqa: ANN001
        ref = uuid.uuid4().hex[:8]
        log.exception("[api] unhandled error on %s (ref=%s)", request.url.path, ref)
        return JSONResponse(
            status_code=500,
            content=_body(
                "internal",
                "The server hit an unexpected error. The log has the detail.",
                ref,
            ),
        )
