"""A successful app update hands the new EA to this machine's terminals.

The pull already brings `mql5/ForexTraderBridge.mq5` to every remote machine --
it is in the repo. What never happened is the step after it: copying that file
into each terminal's MQL5/Experts folder. Until now a remote user had to run
`tools/deploy_ea.sh` (macOS only, and they do not have it) or hand-copy, so an
EA change reached the app on every machine and the EA on none of them.

The wiring is the whole subject here: `ea_deploy` has its own tests
(tests/services/broker/test_ea_deploy.py) for what it does with the files. This
only asks whether `apply_update` actually calls it, and whether a failure in it
can cost a good update -- both things a report cannot tell you and neither
module's own tests can see.

No git command runs and no file is copied: `_run_git` is a scripted recorder
and the deploy is a sentinel.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_app_update as upd
from backend.src.services.broker import ea_deploy


@pytest.fixture
def git(monkeypatch):
    """Every git call succeeds, so apply_update reaches its post-update steps."""
    async def _run_git(*args, **kw):
        return (0, "", "")
    monkeypatch.setattr(upd, "_run_git", _run_git)


@pytest.fixture
def has_checkout(monkeypatch):
    monkeypatch.setattr(upd.Path, "exists", lambda self: True)


@pytest.fixture
def no_restart(monkeypatch):
    monkeypatch.setattr(upd, "_restart", lambda: None)


@pytest.fixture
def quiet_post_steps(monkeypatch):
    """pip install and the pycache sweep are real work on a real checkout. The
    EA deploy is deliberately NOT stubbed here -- it is the subject."""
    monkeypatch.setattr(upd.subprocess, "run", lambda *a, **k: None)
    monkeypatch.setattr(upd.shutil, "rmtree", lambda *a, **k: None)


@pytest.fixture
def deploys(monkeypatch):
    calls = []
    monkeypatch.setattr(ea_deploy, "deploy_after_update",
                        lambda *a, **k: calls.append(True) or {"ok": True})
    return calls


def test_a_successful_update_deploys_the_ea(git, has_checkout, no_restart,
                                            quiet_post_steps, deploys):
    result = asyncio.run(upd.apply_update())

    assert result["ok"] is True
    assert deploys == [True]


def test_a_failed_ea_deploy_does_not_fail_the_update(git, has_checkout, no_restart,
                                                     quiet_post_steps, monkeypatch):
    """The pull has already landed by then. Reporting the update as failed
    would send the user round again for a file copy, and a re-pull is the one
    step that is not safe to retry blindly."""
    def _boom(*a, **k):
        raise RuntimeError("no terminals, no permissions, no anything")
    monkeypatch.setattr(ea_deploy, "deploy_after_update", _boom)

    result = asyncio.run(upd.apply_update())

    assert result["ok"] is True


def test_a_failed_ea_deploy_still_restarts(git, has_checkout, quiet_post_steps,
                                           monkeypatch):
    """Restarting is what makes the pulled code actually run. A live install
    once served 40 minutes of real trades against already-replaced modules
    because a post-update step swallowed the restart."""
    restarts = []
    monkeypatch.setattr(upd, "_restart", lambda: restarts.append(True))
    monkeypatch.setattr(ea_deploy, "deploy_after_update",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))

    asyncio.run(upd.apply_update())

    assert restarts == [True]


def test_a_failed_git_step_never_reaches_the_deploy(has_checkout, no_restart,
                                                    quiet_post_steps, deploys,
                                                    monkeypatch):
    """Negative control, and a real rule: there is no new EA to deploy if the
    pull did not happen. Copying the OLD source into the terminals would reset
    its mtime and make an up-to-date .ex5 look stale."""
    async def _fetch_fails(*args, **kw):
        return (1, "", "fetch failed")
    monkeypatch.setattr(upd, "_run_git", _fetch_fails)

    result = asyncio.run(upd.apply_update())

    assert result["ok"] is False
    assert deploys == []
