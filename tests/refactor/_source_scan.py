"""Where a name appears in the source tree.

Two searches, because two questions are asked of this repo and they are not
the same one:

* `readers_of` finds a name used as a STRING LITERAL -- how an adaptive
  parameter is read (`ap.get("min_rr")`).
* `references_to` finds a name used as an IDENTIFIER -- how an exported
  function is called (`panel_data.change_signature`).

Split out of the tests that use them so the scans can be exercised by their
own negative controls rather than trusted.
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


def references_to(name: str, *, exclude: tuple[str, ...]) -> list[str]:
    """Files mentioning `name` as a whole word, ignoring `exclude`.

    Deliberately looser than a call graph: the name counts whether it is
    called, passed, re-exported or only named in a docstring. A gate built on
    this can say "nothing anywhere mentions this" with confidence, and must
    not claim more than that.
    """
    rx = re.compile(rf"\b{re.escape(name)}\b")
    return [path for path, text in _sources()
            if path not in exclude and rx.search(text)]
