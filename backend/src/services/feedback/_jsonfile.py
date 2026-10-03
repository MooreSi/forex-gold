"""A small JSON list on disk, written whole and atomically.

Feedback is a handful of rows a day at most, so a file beside the other
`remote/` state is the right size. Written to a temp file and renamed so a
crash mid-write leaves the previous list rather than half of a new one.
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

LOCK = threading.RLock()


def read(path: Path) -> list[dict]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [r for r in data if isinstance(r, dict)] if isinstance(data, list) else []


def write(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    os.replace(tmp, path)
