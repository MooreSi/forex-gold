"""Say WHY the two nodes differ, when this machine is the one ahead
(owner, 2026-09-29).

Upgrade VPS pulls origin/main. When the Mac runs commits it has not pushed,
the VPS lands on origin/main and the Remote tab still said "Not in sync: the
nodes run different commits" -- which read as "the upgrade did not take".
It had: the VPS was running the newest commit GitHub had. The report now
carries the commit this machine last saw on origin/main, so the screen can
say "push first" instead.

Nothing here reaches git: the origin reader is replaced.
"""
from __future__ import annotations

from backend.src.services.cluster.sync import _update_sync as us

SHA_MAC = "c" * 40
SHA_ORIGIN = "a" * 40


def test_the_report_carries_the_origin_commit(monkeypatch):
    monkeypatch.setattr(us, "_local", lambda: {"commit": SHA_MAC, "git_version": ""})

    got = us.version_report({"commit": SHA_ORIGIN, "running_commit": SHA_ORIGIN},
                            origin_commit=SHA_ORIGIN)

    assert got["in_sync"] is False
    assert got["origin_commit"] == SHA_ORIGIN


def test_no_origin_commit_is_empty_not_missing(monkeypatch):
    monkeypatch.setattr(us, "_local", lambda: {"commit": SHA_MAC, "git_version": ""})

    assert us.version_report({})["origin_commit"] == ""


def test_the_screen_read_asks_git_for_origin_main(monkeypatch):
    """current_version_report is what the Remote tab reads; it must fill the
    origin commit from this checkout's origin/main ref."""
    monkeypatch.setattr(us, "_local", lambda: {"commit": SHA_MAC, "git_version": ""})
    monkeypatch.setattr(us, "_origin_commit", lambda: SHA_ORIGIN)

    got = us.current_version_report()

    assert got["origin_commit"] == SHA_ORIGIN


def test_an_unreadable_origin_is_empty(monkeypatch):
    def _boom(*a, **k):
        raise OSError("no git")

    monkeypatch.setattr(us.subprocess, "run", _boom)

    assert us._origin_commit() == ""
