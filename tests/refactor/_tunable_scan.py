"""Where an adaptive parameter's name appears in the source tree.

Split out of `test_every_tunable_is_read.py` so the scan can be exercised by
that file's own negative controls rather than being trusted.
"""
from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# The two parameter catalogues. A name appearing only here is a definition, not
# a reader -- excluding them is what makes the scan mean anything.
CATALOGUE_FILES = (
    "backend/src/services/test_signal/adaptive_params.py",
    "backend/src/services/breakout_signal/adaptive_params.py",
)


@lru_cache(maxsize=1)
def _sources() -> tuple[tuple[str, str], ...]:
    out = []
    for root in ("backend", "frontend"):
        for p in (REPO / root).rglob("*.py"):
            out.append((p.relative_to(REPO).as_posix(), p.read_text(encoding="utf-8")))
    return tuple(out)


def readers_of(name: str, *, exclude: tuple[str, ...]) -> list[str]:
    """Files quoting `name` as a string literal, ignoring `exclude`.

    A string-literal match, because that is how every one of these is read:
    `ap.get("min_rr")`. A parameter fetched through a computed name would be
    missed -- none is, and one would be worth objecting to on its own.
    """
    rx = re.compile(rf"""["']{re.escape(name)}["']""")
    return [path for path, text in _sources()
            if path not in exclude and rx.search(text)]
