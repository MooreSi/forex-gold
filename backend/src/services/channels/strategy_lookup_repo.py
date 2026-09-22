"""Finding channels BY the strategy they are set to, rather than by name.

Its own module because `repo.py` is at the 800-line ceiling, and because this
asks a different question from everything in there: every other lookup starts
from a channel, and this one starts from a strategy string and finds the
channels wearing it.

The one caller is `broker/template_rename`. A renamed EA template has to be
repointed wherever it is named, and a channel that is not in the canonical
bucket order would be skipped by `get_all_channel_strategy_overrides`, which
filters to canonical rows.
"""
from __future__ import annotations

import logging

from backend.src.db.database import db

log = logging.getLogger(__name__)

__all__ = ["fetch_sources_using_strategy"]

def fetch_sources_using_strategy(strategy: str) -> list[dict]:
    """Every channel whose assignment or AI recommendation is `strategy`.

    Matched on the whole value, never a substring -- `template:Old Name` and
    `template:Old Name v2` are two different templates, and a LIKE here would
    repoint the second one at the first one's new name.

    Never raises: a rename must not be taken down by a lookup, and an empty
    list degrades to "nothing to repoint", which the caller reports as such.
    """
    if not strategy:
        return []
    out: dict[str, dict] = {}
    try:
        with db() as conn:
            for source, auto in conn.execute(
                "SELECT source, auto_strategy FROM channel_performance "
                "WHERE strategy_override=?", (strategy,),
            ).fetchall():
                out.setdefault(source, {"source": source, "assigned": False,
                                        "recommended": False, "auto": False})
                out[source]["assigned"] = True
                out[source]["auto"] = bool(auto)
            for (source,) in conn.execute(
                "SELECT source FROM channel_strategy_rec WHERE strategy=?",
                (strategy,),
            ).fetchall():
                out.setdefault(source, {"source": source, "assigned": False,
                                        "recommended": False, "auto": False})
                out[source]["recommended"] = True
    except Exception as exc:
        log.debug("[Channels] strategy lookup failed for %r: %s", strategy, exc)
    return list(out.values())
