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

# The parameter catalogues. A name appearing only here is a definition, not a
# reader -- excluding them is what makes the scan mean anything. There were two
# until 2026-09-14, when the Bounce engine and its catalogue were deleted.
CATALOGUE_FILES = (
    "backend/src/services/breakout_signal/adaptive_params.py",
)


@lru_cache(maxsize=1)
def _sources() -> tuple[tuple[str, str], ...]:
    out = []
    # `frontend` was a search root until 2026-09-18. It holds TypeScript now,
    # and a React component never names a Python function -- it asks for a URL.
    # Leaving it in the list would have looked thorough while contributing
    # nothing, which is the shape of a scanner that reassures without checking.
    # The API layer under `backend/src/api/` is where the references moved to,
    # and it is already inside this root.
    for root in ("backend",):
        base = REPO / root
        files = list(base.rglob("*.py"))
        if not files:
            raise AssertionError(
                f"source scan root {root!r} contains no Python — the scan would "
                "report everything as unreferenced (or nothing as referenced) "
                "and mean neither"
            )
        for p in files:
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


_WORD_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


@lru_cache(maxsize=1)
def _tokens() -> tuple[tuple[str, frozenset], ...]:
    """Every identifier-shaped word in each file, once.

    A regex per name over every file is O(names x files) and measured at 40
    seconds for the controller gate's 255 names -- ten per cent of the whole
    suite for one check. Tokenising each file once and testing membership is
    the same answer in under a second.
    """
    return tuple((path, frozenset(_WORD_RE.findall(text)))
                 for path, text in _sources())


def references_to(name: str, *, exclude: tuple[str, ...]) -> list[str]:
    """Files mentioning `name` as a whole word, ignoring `exclude`.

    Deliberately looser than a call graph: the name counts whether it is
    called, passed, re-exported or only named in a docstring. A gate built on
    this can say "nothing anywhere mentions this" with confidence, and must
    not claim more than that.
    """
    return [path for path, words in _tokens()
            if path not in exclude and name in words]
