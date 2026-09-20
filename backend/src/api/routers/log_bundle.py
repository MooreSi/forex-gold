"""Download the filtered logs.

Its own router because `settings.py` is at the ceiling, and because this
returns a file rather than JSON.

**It builds a bundle and hands it to the operator. It sends nothing.** The
NiceGUI original mailed the bundle to a hardcoded address; a browser can just
save the file, which needs no email provider configured and puts nobody's
address in the code.

The bundle carries this machine's app version, environment and platform
alongside its logs, so it is support material about this install. It is
returned to the caller who asked for it and nowhere else.
"""
from __future__ import annotations

from fastapi import APIRouter, Query
from fastapi.responses import PlainTextResponse

from backend.src.api.errors import Refusal
from backend.src.controllers import log_bundle_controller as bundle_ctl

router = APIRouter(prefix="/api/settings", tags=["settings"])

# A month is plenty for a support question and keeps a pathological request
# from reading every rotated file on disk.
MAX_DAYS = 30


@router.get("/log-bundle", response_class=PlainTextResponse)
async def log_bundle(days: int = Query(5, ge=1)) -> PlainTextResponse:
    """The filtered log as a downloadable text file."""
    if days > MAX_DAYS:
        raise Refusal(
            f"{days} days is more than this can export; the limit is {MAX_DAYS}.",
            status_code=400,
        )
    out = bundle_ctl.build_log_bundle(days)
    return PlainTextResponse(
        out["text"],
        headers={
            "Content-Disposition": f'attachment; filename="{out["filename"]}"',
            # Read by the screen so it can say what was in the file without
            # parsing it back out of the download.
            "X-Log-Lines-Kept": str(out["kept_lines"]),
            "X-Log-Lines-Scanned": str(out["raw_lines"]),
            "X-Log-Truncated": "1" if out["truncated"] else "0",
        },
    )
