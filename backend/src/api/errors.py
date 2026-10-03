"""One error shape for the whole API, and one rule about what reaches the user.

The money rule from the frontend conventions is the reason this module is not
just `raise HTTPException`:

    Surface a rejection verbatim. If the backend refuses an order, the user
    needs the real reason, not "something went wrong".

So there are two kinds of failure and they are treated differently:

* `Refusal` — the backend considered the request and said no. A trading halt,
  a disconnected bridge, an out-of-hours window, a broker rejection. The text
  IS the answer and it goes to the client unchanged.
* `RequestValidationError` — the request did not match the route's model.
  A 422 with kind "invalid", plus FastAPI's usual `detail` list so the field
  that failed is named.
* anything else — an unexpected exception. The client gets a generic message
  and a correlation id; the detail goes to the log. An AttributeError's text
  is not information the user can act on, and it leaks internals.

Both produce the same JSON body, so the client has one thing to parse:

    {"error": {"kind": "refusal"|"invalid"|"internal", "message": str, "ref": str|null}}

The 422 handler replaces FastAPI's default because the default echoes each
error's `input` back. A NaN or Infinity sent as a JSON literal is accepted on
the way in and cannot be serialised on the way out, which turned a malformed
request into a 500 with a correlation id.
"""
from __future__ import annotations

import logging
import math
import uuid

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
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


def _json_safe(value):
    """Non-finite floats become their repr, exceptions their message."""
    if isinstance(value, float) and not math.isfinite(value):
        return repr(value)
    if isinstance(value, BaseException):
        return str(value)
    if isinstance(value, dict):
        return {k: _json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(v) for v in value]
    return value


def _summary(errors: list[dict]) -> str:
    if not errors:
        return "The request was not valid."
    first = errors[0]
    where = ".".join(str(p) for p in first.get("loc", ()))
    more = f" (and {len(errors) - 1} more)" if len(errors) > 1 else ""
    return f"Invalid request: {where}: {first.get('msg', 'invalid')}{more}"


def install(app: FastAPI) -> None:
    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, exc: RequestValidationError):  # noqa: ANN001
        detail = jsonable_encoder(_json_safe(list(exc.errors())))
        return JSONResponse(status_code=422, content={
            **_body("invalid", _summary(detail)), "detail": detail,
        })

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
