"""Signal commentary and tg-signal row maintenance for the Trading page.

`set_commentary` serialises to JSON here rather than in the controller. The
storage format is this service's business, and a controller that knows the
column holds JSON is a controller that can write a different shape into it.
"""
from __future__ import annotations

import json

from backend.src.services.signals import repo as _repo
from backend.src.services.signals import tg_repo as _tg_repo

__all__ = ["get_signal", "set_commentary", "delete_tg_row"]


def get_signal(signal_id: str) -> dict:
    return _repo.get_signal(signal_id)


def set_commentary(signal_id: str, commentary: dict) -> None:
    _repo.set_signal_commentary(signal_id, json.dumps(commentary))


def delete_tg_row(row_id) -> None:
    _tg_repo.delete_tg_signal_row(row_id)
