"""tools/publish_release.py: a version bump becomes a GitHub release.

The release body is the same shape as v0.611's: a heading, a pointer to the
changelog, then the engineering record for that version.
"""
from pathlib import Path

import pytest

from tools import publish_release as pr

CHANGELOG = """# Changelog

## v0.612 — The VPS trades Telegram itself (2026-09-29)

**Telegram**
- one

## v0.611 — Windows install and VPS setup (2026-09-28)

**Installer**
- two
"""


def test_the_section_for_a_version_stops_at_the_next_heading():
    got = pr.changelog_section(CHANGELOG, "v0.612")
    assert "**Telegram**" in got and "- one" in got
    assert "v0.611" not in got and "- two" not in got


def test_v0_61_is_not_confused_with_v0_612():
    text = "## v0.612 — a\n\nx\n\n## v0.61 — b\n\ny\n"
    assert pr.changelog_section(text, "v0.61").strip() == "y"


def test_a_version_with_no_section_is_none():
    assert pr.changelog_section(CHANGELOG, "v0.9") is None


def test_the_body_uses_the_changelog_when_there_is_one():
    body = pr.release_body("v0.612", "Title", ["a bullet"], CHANGELOG)
    assert body.startswith("## v0.612 — Title")
    assert "CHANGELOG.md" in body
    assert "- one" in body and "a bullet" not in body


def test_the_body_falls_back_to_the_about_screen_bullets():
    body = pr.release_body("v0.9", "Title", ["first", "second"], CHANGELOG)
    assert "- first" in body and "- second" in body


def test_the_current_release_is_the_first_entry_of_the_real_file():
    tag, title, bullets = pr.current_release(
        Path(pr.REPO) / "backend/src/utils/version_history.py")
    assert tag.startswith("v") and title and bullets


def test_an_existing_release_is_not_recreated(monkeypatch):
    calls = []
    monkeypatch.setattr(pr, "_gh", lambda *a: calls.append(a) or 0)
    assert pr.publish("v0.612", "T", "body", "abc") == "exists"
    assert [c[0] for c in calls] == ["release"] and calls[0][1] == "view"


def test_a_missing_release_is_created_on_the_pushed_commit(monkeypatch):
    calls = []

    def fake(*a):
        calls.append(a)
        return 1 if a[1] == "view" else 0

    monkeypatch.setattr(pr, "_gh", fake)
    assert pr.publish("v0.612", "T", "body", "abc123") == "created"
    create = calls[-1]
    assert create[1] == "create" and "abc123" in create and "--latest" in create


def test_a_failed_create_raises(monkeypatch):
    monkeypatch.setattr(pr, "_gh", lambda *a: 1)
    with pytest.raises(RuntimeError):
        pr.publish("v0.612", "T", "body", "abc")
