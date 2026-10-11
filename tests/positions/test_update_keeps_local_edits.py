"""Update must not throw away uncommitted edits.

Owner, 2026-10-11: the in-app Update at 07:05 ran `checkout -f` over the
owner's own checkout and discarded three sessions of uncommitted work (the
wider equity curve, the dashboard's 1D/1W/30D/All window, the trading-clock
day). New files survived; every edit to an existing file was reset.

Update still lands on origin exactly -- a client that has drifted must still
update -- but the edits are put in `git stash` first, where `git stash list`
finds them. If they cannot be put there, the update stops and changes nothing.

These run real git against a local "origin", no network.
"""
from __future__ import annotations

import asyncio
import subprocess

import pytest

from backend.src.services.broker import ea_deploy
from backend.src.services.positions import core_app_update as upd
from backend.src.services.positions import core_update_set_aside as set_aside


def _git(cwd, *args) -> str:
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True,
    ).stdout


@pytest.fixture
def checkout(tmp_path, monkeypatch):
    """A clone one commit behind its origin, wired in as the app's repo."""
    origin = tmp_path / "origin"
    origin.mkdir()
    _git(origin, "init", "-q", "-b", "main")
    (origin / "app.py").write_text("v1\n")
    _git(origin, "add", "app.py")
    _git(origin, "commit", "-q", "-m", "one")
    root = tmp_path / "repo"
    _git(tmp_path, "clone", "-q", str(origin), str(root))
    (origin / "app.py").write_text("v2\n")
    _git(origin, "commit", "-q", "-am", "two")

    monkeypatch.setattr(upd, "_REPO_ROOT", root)
    monkeypatch.setattr(upd, "_restart", lambda: None)
    monkeypatch.setattr(ea_deploy, "deploy_after_update", lambda *a, **k: {"ok": True})
    return root


def test_an_uncommitted_edit_is_stashed_not_discarded(checkout):
    (checkout / "app.py").write_text("v1 plus the owner's edit\n")

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is True
    assert (checkout / "app.py").read_text() == "v2\n", "the update must still land"
    stashed = _git(checkout, "stash", "list")
    assert stashed.count("\n") == 1, stashed
    assert "the owner's edit" in _git(checkout, "stash", "show", "-p", "stash@{0}")


def test_a_clean_checkout_leaves_no_stash(checkout):
    out = asyncio.run(upd.apply_update())

    assert out["ok"] is True
    assert (checkout / "app.py").read_text() == "v2\n"
    assert _git(checkout, "stash", "list") == ""


def test_if_the_edits_cannot_be_set_aside_the_update_changes_nothing(checkout, monkeypatch):
    (checkout / "app.py").write_text("v1 plus the owner's edit\n")
    real = set_aside._git

    def _stash_refused(root, *args):
        if args[:1] == ("stash",):
            return 1, "", "stash refused"
        return real(root, *args)
    monkeypatch.setattr(set_aside, "_git", _stash_refused)

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is False
    assert "stash refused" in out["error"]
    assert (checkout / "app.py").read_text() == "v1 plus the owner's edit\n"
