"""The committed bundle has to build identically on every platform.

`frontend/dist` is committed and is what the app serves, and CI rebuilds it and
fails on any diff. That check is only worth having if the build is
deterministic across machines — and it was not.

Vite builds `dist/index.html` from `frontend/index.html`, passing the lines it
does not touch straight through. Git for Windows defaults `core.autocrlf` to
true, so a Windows runner checked the source out with CRLF and the output came
out with CRLF on 13 of its 15 lines, against a committed copy with LF. On
2026-09-18 CI reported **"frontend/dist is stale"** while its own build produced
exactly the committed content-hashed asset names: the bundle was identical and
the line endings were not.

Reproduced locally by converting the source to CRLF and rebuilding — 13
insertions, 13 deletions, the same stat CI printed — and fixed by pinning the
source to LF in `.gitattributes`.

These tests ask **git** what the attributes resolve to rather than reading the
file, because a pattern that does not match is the failure worth catching and
the file's text looks equally convincing either way.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]


def _attrs(path: str) -> dict[str, str]:
    """What git says `text` and `eol` resolve to for a path."""
    out = subprocess.run(
        ["git", "check-attr", "text", "eol", "--", path],
        cwd=REPO, capture_output=True, text=True, check=True).stdout
    found = {}
    for line in out.splitlines():
        _path, name, value = line.rsplit(": ", 2)
        found[name] = value
    return found


def test_the_html_template_is_checked_out_with_lf_everywhere():
    """The actual fix. The build inherits the source's line endings, so the
    source has to be the same bytes on every platform."""
    attrs = _attrs("frontend/index.html")

    assert attrs["text"] == "set"
    assert attrs["eol"] == "lf"


def test_the_built_bundle_is_never_converted():
    """A build artefact has no line-ending policy; it has bytes."""
    for path in ("frontend/dist/index.html",
                 "frontend/dist/assets/favicon-CzvPAnd2.png"):
        assert _attrs(path)["text"] == "unset", path


def test_the_bundle_is_not_marked_as_text():
    """`text eol=lf` on `dist/**` was the first attempt and it silently
    rewrote the favicon, 3770 bytes to 3769 — the PNG happened to contain a
    CRLF byte pair. `-text` is what a directory of mixed binary and text build
    output needs."""
    assert _attrs("frontend/dist/assets/favicon-CzvPAnd2.png")["eol"] == "unspecified"


def test_the_committed_template_really_has_no_carriage_returns():
    """The attribute governs future checkouts; this is the blob as it stands."""
    blob = subprocess.run(
        ["git", "show", "HEAD:frontend/index.html"],
        cwd=REPO, capture_output=True, check=True).stdout

    assert b"\r" not in blob


def test_the_committed_bundle_html_has_none_either():
    blob = subprocess.run(
        ["git", "show", "HEAD:frontend/dist/index.html"],
        cwd=REPO, capture_output=True, check=True).stdout

    assert b"\r" not in blob


def test_the_reader_can_tell_a_pinned_path_from_an_unpinned_one():
    """Negative control. Every assertion above would pass for a `check-attr`
    that returned the same answer for everything."""
    backend = _attrs("backend/src/app.py")

    assert backend["eol"] == "unspecified", (
        "the repo now pins line endings everywhere — that is a much bigger "
        "change than this file describes, and these tests no longer say what "
        "they claim")


@pytest.mark.parametrize("path", ["frontend/index.html", "frontend/dist/index.html"])
def test_the_paths_these_pin_still_exist(path):
    """A pattern pointing at a moved file resolves to nothing and passes."""
    assert (REPO / path).is_file(), path
