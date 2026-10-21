"""Response shapes for /api/system."""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, model_validator


class ReleaseOut(BaseModel):
    """One About > Version history entry.

    `version_history.RELEASES` holds `(version, title, colour, badge,
    [changes])` tuples, which JSON writes as bare arrays; the About screen
    reads names. The version drops its "v" so it compares equal to the
    running version (`app_version()` is "0.615", the tuple says "v0.615").
    """
    version: str
    title: str = ""
    badge: str = ""
    notes: list[str]

    @model_validator(mode="before")
    @classmethod
    def _from_tuple(cls, raw: Any) -> Any:
        if isinstance(raw, (tuple, list)):
            version, title, _colour, badge, notes = raw
            return {"version": str(version).lstrip("v"), "title": title,
                    "badge": badge, "notes": list(notes)}
        return raw


class ReleasesOut(BaseModel):
    version: str
    releases: list[ReleaseOut]
