"""The one place a training reader picks up imported rows.

`with_imported(kind, own_rows)` returns `own_rows` itself, untouched, unless
the kind's switch is on. On, it adds the imported rows that are not an event
this node already holds and puts the whole list in time order (oldest first,
own before imported on a tie), which is what every reader's weighting assumes.
"""
from __future__ import annotations

import logging
from typing import Optional

from backend.src.services.ml_merge import kinds, repo

log = logging.getLogger(__name__)


def merge(kind: str, own: list[dict], peer: list[dict]) -> list[dict]:
    """`own` plus the non-duplicate `peer` rows, oldest first."""
    fresh = kinds.drop_duplicates(kind, own, peer)
    if not fresh:
        return list(own)
    key = kinds.ORDER[kind]
    tagged = ([(key(r), 0, i, r) for i, r in enumerate(own)]
              + [(key(r), 1, i, r) for i, r in enumerate(fresh)])
    tagged.sort(key=lambda t: t[:3])
    return [t[3] for t in tagged]


def peer_rows(kind: str, background: Optional[bool] = None) -> list[dict]:
    """The imported rows of `kind`; for `pro`, positives or background only."""
    rows = repo.imported(kind)
    if kind == "pro" and background is not None:
        rows = [r for r in rows if (r.get("stage") == "background") == background]
    return rows


def with_imported(kind: str, own_rows: list[dict],
                  background: Optional[bool] = None) -> list[dict]:
    if not repo.use_imported(kind):
        return own_rows
    try:
        return merge(kind, own_rows, peer_rows(kind, background))
    except Exception as exc:
        log.warning("[ml_merge] %s: imported rows left out: %s", kind, exc)
        return own_rows
