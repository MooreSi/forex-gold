"""The update screen says which repository and branch it is comparing against.

Found while wiring the Node & Updates card on 2026-09-20. This checkout's
`origin` is `MooreSi/forex-react`, `_BRANCH` is "main", and the app runs on
`react-dashboard` -- so "Up to date" is true of a branch nobody is running,
and the card's hardcoded link pointed at `MooreSi/forex`, a different
repository entirely.

Choosing which branch an install follows is the owner's call, not a bug to be
fixed quietly. What the screen can do is stop implying it compared against
something it did not.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_app_update as upd


@pytest.fixture
def git(monkeypatch):
    calls = []

    async def _run(*args, timeout=30.0):
        calls.append(args)
        if args[:2] == ("remote", "get-url"):
            return 0, "https://github.com/MooreSi/forex-react.git\n", ""
        return 1, "", "no"

    monkeypatch.setattr(upd, "_run_git", _run)
    upd.reset_update_cache()
    return calls


def test_it_names_the_branch_it_compares_against(git):
    assert asyncio.run(upd.tracking())["branch"] == upd._BRANCH


def test_it_names_the_repository_origin_actually_points_at(git):
    """Not the constant. The constant is what a bootstrap would CREATE; it is
    not evidence of what this checkout fetches from."""
    assert asyncio.run(upd.tracking())["repo_url"] \
        == "https://github.com/MooreSi/forex-react"


def test_a_checkout_with_no_remote_says_nothing_rather_than_guessing(monkeypatch):
    async def _run(*args, timeout=30.0):
        return 128, "", "No such remote 'origin'"

    monkeypatch.setattr(upd, "_run_git", _run)

    assert asyncio.run(upd.tracking())["repo_url"] == ""
