"""GitHub-based app self-update.

`core_app_update.py` was 60% covered, with the two functions that matter most
almost entirely untested: `check_for_update` (the header's "Update Available"
badge and the Update page) and `apply_update` (the button both the client and
the admin console converge on).

**No git command runs.** `_run_git` is replaced with a scripted recorder, so the
tests drive the decision logic without fetching, checking out, or touching this
working tree. `apply_update` force-checkouts over local changes by design; a
test that really invoked it would discard whatever was in progress.

The behaviour worth pinning is the failure handling. Both functions return an
error dict instead of raising -- `check_for_update` because the page calls it on
a timer and "a flaky connection never breaks the page", `apply_update` because
its caller shows the result and decides whether to restart. Silent success on a
failed pull is the bad outcome: the user restarts into the old code believing it
updated.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_app_update as upd


class _Git:
    """A scripted `git`. Each entry is (returncode, stdout, stderr), matched to
    calls in order; the call log is the assertion surface."""

    def __init__(self, *results):
        self.results = list(results)
        self.calls = []

    async def __call__(self, *args, timeout=30.0):
        self.calls.append(args)
        if not self.results:
            return 0, "", ""
        return self.results.pop(0)

    @property
    def commands(self):
        return [a[0] for a in self.calls]


@pytest.fixture
def git(monkeypatch):
    def _install(*results):
        g = _Git(*results)
        monkeypatch.setattr(upd, "_run_git", g)
        return g
    return _install


@pytest.fixture
def has_checkout(monkeypatch, tmp_path):
    """Point the module at a directory that does or does not contain .git."""
    def _set(exists: bool):
        root = tmp_path / "repo"
        (root / ".git").mkdir(parents=True) if exists else root.mkdir(parents=True)
        monkeypatch.setattr(upd, "_REPO_ROOT", root)
        return root
    return _set


# ── check_for_update ──────────────────────────────────────────────────────────

def test_no_checkout_reports_that_rather_than_failing(has_checkout, git):
    has_checkout(False)
    g = git()
    out = asyncio.run(upd.check_for_update())

    assert out == {"available": False, "error": "not a git checkout"}
    assert g.calls == [], "nothing should be run without a checkout"


def test_a_failed_fetch_is_reported_not_raised(has_checkout, git):
    """The Update page polls this on a timer; an exception would break it."""
    has_checkout(True)
    git((128, "", "fatal: unable to access remote"))

    out = asyncio.run(upd.check_for_update())

    assert out["available"] is False
    assert "unable to access remote" in out["error"]


def test_being_up_to_date_is_not_an_update(has_checkout, git):
    has_checkout(True)
    same = "a" * 40
    git((0, "", ""), (0, same, ""), (0, same, ""))

    out = asyncio.run(upd.check_for_update())

    assert out["available"] is False
    assert out["error"] is None
    assert out["commits"] == []
    assert out["local_sha"] == out["remote_sha"] == same


def test_a_difference_is_reported_with_its_commits_newest_first(has_checkout, git):
    has_checkout(True)
    local, remote = "a" * 40, "b" * 40
    log = "\x1f".join(["b" * 40, "bbbbbbb", "newest change"]) + "\n" + \
          "\x1f".join(["c" * 40, "ccccccc", "older change"])
    git((0, "", ""), (0, local, ""), (0, remote, ""), (0, log, ""))

    out = asyncio.run(upd.check_for_update())

    assert out["available"] is True
    assert out["error"] is None
    assert [c["summary"] for c in out["commits"]] == ["newest change", "older change"]
    assert out["commits"][0]["short_sha"] == "bbbbbbb"


def test_an_unreadable_log_still_reports_the_update(has_checkout, git):
    """Knowing an update exists matters more than being able to list it."""
    has_checkout(True)
    git((0, "", ""), (0, "a" * 40, ""), (0, "b" * 40, ""), (1, "", "log failed"))

    out = asyncio.run(upd.check_for_update())

    assert out["available"] is True
    assert out["commits"] == []


def test_a_malformed_log_line_is_skipped_not_guessed_at(has_checkout, git):
    has_checkout(True)
    log = "not-in-the-expected-format\n" + "\x1f".join(["b" * 40, "bbbbbbb", "good one"])
    git((0, "", ""), (0, "a" * 40, ""), (0, "b" * 40, ""), (0, log, ""))

    out = asyncio.run(upd.check_for_update())

    assert [c["summary"] for c in out["commits"]] == ["good one"]


def test_an_unresolvable_head_is_reported(has_checkout, git):
    has_checkout(True)
    git((0, "", ""), (128, "", "bad object"))

    out = asyncio.run(upd.check_for_update())
    assert out["error"] == "could not resolve local HEAD"


# ── apply_update ──────────────────────────────────────────────────────────────

@pytest.fixture
def no_post_steps(monkeypatch):
    """pip install and the pycache sweep are real filesystem work; the decision
    logic under test is the git sequence before them."""
    async def _to_thread(fn, *a, **k):
        return None
    monkeypatch.setattr(upd.asyncio, "to_thread", _to_thread)


def test_a_fresh_install_is_bootstrapped_before_fetching(has_checkout, git, no_post_steps, monkeypatch):
    """The Setup & Start scripts copy files, they never clone -- so a newly
    installed machine has no .git until its first update."""
    has_checkout(False)
    monkeypatch.setattr(upd.shutil, "which", lambda _: "/usr/bin/git")
    g = git()

    out = asyncio.run(upd.apply_update())

    assert out == {"ok": True, "error": None}
    assert g.commands[:2] == ["init", "remote"], "a checkout must be created first"
    assert "fetch" in g.commands and "checkout" in g.commands


def test_no_git_on_path_cannot_bootstrap(has_checkout, git, no_post_steps, monkeypatch):
    has_checkout(False)
    monkeypatch.setattr(upd.shutil, "which", lambda _: None)
    g = git()

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is False
    assert "git not found" in out["error"]
    assert g.calls == []


def test_an_existing_checkout_is_not_re_initialised(has_checkout, git, no_post_steps):
    has_checkout(True)
    g = git()

    asyncio.run(upd.apply_update())

    assert "init" not in g.commands


def test_the_working_tree_is_forced_to_match_origin(has_checkout, git, no_post_steps):
    """`checkout -B --track -f`, not `pull --ff-only`. No client carries its own
    commits; each mirrors origin, so discarding local drift is the intended
    outcome rather than data loss."""
    has_checkout(True)
    g = git()

    asyncio.run(upd.apply_update())

    checkout = next(a for a in g.calls if a[0] == "checkout")
    assert "-B" in checkout and "--track" in checkout and "-f" in checkout


def test_a_failed_fetch_stops_before_touching_the_working_tree(has_checkout, git, no_post_steps):
    has_checkout(True)
    g = git((128, "", "network down"))

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is False
    assert "network down" in out["error"]
    assert "checkout" not in g.commands, "a failed fetch must not proceed to checkout"


def test_a_failed_checkout_is_reported_as_a_failure(has_checkout, git, no_post_steps):
    has_checkout(True)
    git((0, "", ""), (1, "", "checkout refused"))

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is False
    assert "checkout refused" in out["error"]


def test_a_post_update_step_failing_is_a_warning_not_a_failure(has_checkout, git, monkeypatch):
    """The pull already succeeded. Reporting failure would invite a blind retry
    of something that is not safe to repeat, when the real fix is another Save
    & Restart."""
    has_checkout(True)
    git()

    async def _boom(fn, *a, **k):
        raise RuntimeError("pip exploded")
    monkeypatch.setattr(upd.asyncio, "to_thread", _boom)

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is True, "the code did land, so this is not a failed update"
    assert "post-update step failed" in out["error"]
    assert "pip exploded" in out["error"]


# ── apply_update restarts on success ────────────────────────────────────────
# 2026-09-03: a live install was left running for ~40 minutes against a venv
# apply_update() had already reinstalled dependencies into, because every
# caller was individually responsible for restarting afterward and the
# instrumentation to know one of them hadn't (or had failed before reaching
# that step) didn't exist -- every outbound HTTPS call (Telegram alerts, AI
# commentary, the daily email) failed with a stale-module FileNotFoundError
# for that whole window while 5 real trades opened with zero notification.
# apply_update() now restarts itself so no caller can skip or fumble it.

@pytest.fixture
def restart(monkeypatch):
    """Stub the actual relaunch — a real one spawns a subprocess and asks a
    (nonexistent, in a test) NiceGUI server to shut down."""
    calls = []
    monkeypatch.setattr(upd, "_restart", lambda: calls.append(True))
    return calls


def test_a_successful_update_restarts_the_app(has_checkout, git, no_post_steps, restart):
    has_checkout(True)
    git()

    out = asyncio.run(upd.apply_update())

    assert out == {"ok": True, "error": None}
    assert restart == [True], "a landed update must not be left running the old code"


def test_a_soft_post_update_failure_still_restarts(has_checkout, git, monkeypatch, restart):
    """The checkout landed even though pip/pycache-clear didn't -- restarting
    is exactly what 'Save & Restart again' meant, so do it automatically
    rather than leaving the caller to notice and click it."""
    has_checkout(True)
    git()

    async def _boom(fn, *a, **k):
        raise RuntimeError("pip exploded")
    monkeypatch.setattr(upd.asyncio, "to_thread", _boom)

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is True
    assert restart == [True]


def test_a_failed_fetch_does_not_restart(has_checkout, git, no_post_steps, restart):
    has_checkout(True)
    git((128, "", "network down"))

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is False
    assert restart == [], "nothing landed -- restarting would just relaunch the old code"


def test_a_failed_checkout_does_not_restart(has_checkout, git, no_post_steps, restart):
    has_checkout(True)
    git((0, "", ""), (1, "", "checkout refused"))

    out = asyncio.run(upd.apply_update())

    assert out["ok"] is False
    assert restart == []


def test_restart_can_be_suppressed_by_the_caller(has_checkout, git, no_post_steps, restart):
    """The admin-console-triggered update (cluster/remote/_update.py) runs its
    own restart sequence afterward -- Windows icon refresh, then a hard
    os._exit() with the bat-loop's exit code -- and must keep doing exactly
    that rather than being pre-empted by this one."""
    has_checkout(True)
    git()

    out = asyncio.run(upd.apply_update(restart=False))

    assert out == {"ok": True, "error": None}
    assert restart == []


def test_repo_root_resolves_to_the_actual_checkout_root():
    """_REPO_ROOT is computed once at import time from this module's own
    location. The `has_checkout` fixture monkeypatches it directly in every
    other test, which hides a wrong parent-count -- this test is the one
    place that would catch this module resolving to the wrong directory
    after being moved, the way os_utils.repo_root()'s docstring describes
    happening to four other modules on 2026-08-25."""
    from backend.src.utils import os_utils

    assert upd._REPO_ROOT == os_utils.repo_root()
    assert (upd._REPO_ROOT / "run.py").exists()
