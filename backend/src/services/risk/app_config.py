"""App-configuration service: the key/value store five pages read and write.

Thin over `app_config_repo`, and deliberately still a service rather than
letting controllers call the repo. The repo is where the SQL lives and where
cache invalidation is enforced; a controller reaching past this module would
make those invariants a matter of convention again.

The `_async` variants exist because the pages call these from `ui.timer`
callbacks. Off-loop dispatch belongs here, not in the controller: a controller
that owns `to_db_thread` has to import `backend.src.db`, and that import is
what let controllers skip the service layer entirely.
"""
from __future__ import annotations

from typing import Optional

from backend.src.db.database import to_db_thread
from backend.src.services.risk import app_config_repo as _repo

__all__ = ["get", "set", "get_async", "set_async"]


def get(key: str) -> Optional[str]:
    return _repo.get_app_config(key)


def set(key: str, value: str) -> None:  # noqa: A001 - reads as app_config.set(...)
    _repo.set_app_config(key, value)


async def get_async(key: str) -> Optional[str]:
    return await to_db_thread(_repo.get_app_config, key)


async def set_async(key: str, value: str) -> None:
    return await to_db_thread(_repo.set_app_config, key, value)
