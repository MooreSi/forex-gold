"""Linking a downloaded install to GitHub without touching a line of its code.

The installers copy files; they never clone. So a fresh download has no `.git`,
and until 2026-09-12 the Update page answered that with a manual **Set Up
Updates** button -- a step the owner should never have had to find, and one
that ran `apply_update()`, which force-checkouts origin's HEAD over the working
tree. Pressing it on a machine whose files are three commits old is a silent
code update on a machine that may be trading.

`link_checkout()` does the honest half only. It creates the repository, fetches
origin, and then claims the commit **whose tree the installed files already
are** -- found by comparing content, not by trusting that a download is current.
HEAD then tells the truth, `check_for_update()` works, the badge is right, and
not one byte of the working tree has moved. If no recent commit matches, it puts
the `.git` it made back in the bin and leaves the manual button to it, because
the alternative is claiming a commit this install is not.

File modes are excluded from the comparison on purpose: unzipping a GitHub
archive can drop the executable bit from the `.command` launchers, which would
otherwise make a byte-identical download match nothing at all.
"""
from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

import pytest

from backend.src.services.positions import core_app_update as upd


class _GitScript:
    """A `git` that answers by subcommand rather than by call order, so a test
    says what the repository looks like instead of counting calls."""

    def __init__(self, answers: dict):
        self.answers = answers
        self.calls: list[tuple] = []

    async def __call__(self, *args, timeout=30.0):
        self.calls.append(args)
        key = args[0]
        if key == "ls-tree":
            answer = self.answers.get(("ls-tree", args[-1]), (128, "", "unknown rev"))
        else:
            answer = self.answers.get(key, (0, "", ""))
        return answer(*args) if callable(answer) else answer

    @property
    def commands(self) -> list[str]:
        return [a[0] for a in self.calls]


@pytest.fixture
def repo(monkeypatch, tmp_path):
    """Point the module at a directory that does or does not have a checkout."""
    def _set(has_git: bool):
        root = tmp_path / "install"
        root.mkdir(parents=True)
        if has_git:
            (root / ".git").mkdir()
        monkeypatch.setattr(upd, "_REPO_ROOT", root)
        return root
    return _set


@pytest.fixture
def git(monkeypatch):
    """`git init` really does create a .git directory, so the give-up paths
    below are removing something that exists -- which is the thing they are
    about."""
    def _install(answers: dict):
        answers = dict(answers)
        answers.setdefault("init", lambda *_a, **_k: (
            (upd._REPO_ROOT / ".git").mkdir(exist_ok=True), (0, "", ""))[1])
        g = _GitScript(answers)
        monkeypatch.setattr(upd, "_run_git", g)
        return g
    return _install


@pytest.fixture
def git_exists(monkeypatch):
    monkeypatch.setattr(upd.shutil, "which", lambda _n: "/usr/bin/git")


def _index(*pairs) -> str:
    return "".join(f"100644 {sha} 0\t{path}\n" for path, sha in pairs)


def _tree(*pairs) -> str:
    return "".join(f"100644 blob {sha}\t{path}\n" for path, sha in pairs)


FILES = (("run.py", "aaa"), ("backend/src/app.py", "bbb"))


def _answers(head_order, trees, **extra):
    answers = {
        "log": (0, "\n".join(head_order), ""),
        "ls-files": (0, _index(*FILES), ""),
    }
    answers.update({("ls-tree", sha): (0, body, "") for sha, body in trees.items()})
    answers.update(extra)
    return answers


class TestWhenThereIsNothingToDo:
    def test_an_install_that_already_has_a_checkout_is_left_alone(self, repo, git, git_exists):
        repo(True)
        g = git({})

        result = asyncio.run(upd.link_checkout())

        assert result["linked"] is False
        assert result["reason"] == "already-linked"
        assert g.calls == []

    def test_without_a_git_binary_nothing_is_created(self, repo, git, monkeypatch):
        root = repo(False)
        monkeypatch.setattr(upd.shutil, "which", lambda _n: None)
        g = git({})

        result = asyncio.run(upd.link_checkout())

        assert result["reason"] == "no-git"
        assert g.calls == []
        assert not (root / ".git").exists()


class TestClaimingTheRightCommit:
    def test_it_lands_on_the_commit_whose_tree_the_files_actually_are(
        self, repo, git, git_exists,
    ):
        """The whole point. A download that is three commits old must report
        the commit it IS, not the one origin happens to be at -- otherwise the
        admin console badges it up to date while it runs older code."""
        repo(False)
        g = git(_answers(
            ["newer222", "olderMATCH", "older333"],
            {
                "newer222": _tree(("run.py", "zzz"), ("backend/src/app.py", "bbb")),
                "olderMATCH": _tree(*FILES),
                "older333": _tree(("run.py", "qqq")),
            },
        ))

        result = asyncio.run(upd.link_checkout())

        assert result["linked"] is True
        assert result["sha"] == "olderMATCH"
        assert ("update-ref", "refs/heads/main", "olderMATCH") in g.calls

    def test_the_working_tree_is_never_moved(self, repo, git, git_exists):
        """`apply_update()` force-checkouts by design; this must not. The
        install keeps running exactly the code it was running."""
        repo(False)
        g = git(_answers(["onlyone"], {"onlyone": _tree(*FILES)}))

        asyncio.run(upd.link_checkout())

        assert "checkout" not in g.commands
        assert not any(a[0] == "reset" and "--hard" in a for a in g.calls)

    def test_head_ends_up_on_a_branch_that_tracks_origin(self, repo, git, git_exists):
        """A detached HEAD would leave `git fetch`/`rev-parse origin/main`
        working but nothing for a later update to fast-forward."""
        repo(False)
        g = git(_answers(["onlyone"], {"onlyone": _tree(*FILES)}))

        asyncio.run(upd.link_checkout())

        assert ("symbolic-ref", "HEAD", "refs/heads/main") in g.calls
        assert ("branch", "--set-upstream-to=origin/main", "main") in g.calls

    def test_a_dropped_executable_bit_still_matches(self, repo, git, git_exists):
        """Unzipping a GitHub archive can land `FOREX Start.command` as 100644
        where the tree has 100755. The content is identical and the install is
        that commit; comparing modes would say it is no commit at all."""
        repo(False)
        g = git(_answers(
            ["onlyone"],
            {"onlyone": "100755 blob aaa\trun.py\n100644 blob bbb\tbackend/src/app.py\n"},
        ))

        result = asyncio.run(upd.link_checkout())

        assert result["linked"] is True


class TestWhenItCannotBeSureAndSaysSo:
    def test_a_tree_matching_nothing_recent_gives_up_rather_than_guessing(
        self, repo, git, git_exists,
    ):
        root = repo(False)
        g = git(_answers(["a1", "b2"], {
            "a1": _tree(("run.py", "different")),
            "b2": _tree(("run.py", "also-different")),
        }))

        result = asyncio.run(upd.link_checkout())

        assert result["linked"] is False
        assert result["reason"] == "no-matching-commit"
        assert "update-ref" not in g.commands

    def test_it_removes_the_half_built_repository_it_made(self, repo, git, git_exists):
        """A `.git` with an unborn HEAD is worse than none: `check_for_update()`
        stops offering the bootstrap and fails on `rev-parse HEAD` instead."""
        root = repo(False)
        git(_answers(["a1"], {"a1": _tree(("run.py", "different"))}))

        asyncio.run(upd.link_checkout())

        assert not (root / ".git").exists()

    def test_a_fetch_that_cannot_reach_github_leaves_nothing_behind(
        self, repo, git, git_exists,
    ):
        root = repo(False)
        g = git({"fetch": (128, "", "could not resolve host github.com")})

        result = asyncio.run(upd.link_checkout())

        assert result["linked"] is False
        assert result["reason"] == "fetch-failed"
        assert not (root / ".git").exists()
        assert "ls-tree" not in g.commands


class TestAgainstRealGit:
    """One end-to-end pass with the actual binary. The scripted tests above
    pin the decisions; this pins that the decisions match what git does --
    `ls-files -s` and `ls-tree -r` really do agree on a blob sha, and
    `update-ref` + `symbolic-ref` + `reset --mixed` really do leave a usable
    checkout with an untouched working tree."""

    @staticmethod
    def _git(cwd, *args):
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True,
                              text=True, timeout=30)

    @pytest.fixture
    def origin(self, tmp_path):
        # Named with the .git suffix the module appends to _GITHUB_REPO_URL,
        # so the fetch below resolves to this directory.
        src = tmp_path / "origin.git"
        src.mkdir()
        self._git(src, "init", "-b", "main")
        self._git(src, "config", "user.email", "t@t.t")
        self._git(src, "config", "user.name", "t")
        (src / "run.py").write_text("first\n", encoding="utf-8")
        (src / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        self._git(src, "add", "-A")
        self._git(src, "commit", "-m", "one")
        first = self._git(src, "rev-parse", "HEAD").stdout.strip()
        (src / "run.py").write_text("second\n", encoding="utf-8")
        self._git(src, "add", "-A")
        self._git(src, "commit", "-m", "two")
        return src, first

    def test_an_old_download_links_to_its_own_commit_untouched(
        self, origin, tmp_path, monkeypatch, git_exists,
    ):
        src, first_sha = origin
        install = tmp_path / "install"
        install.mkdir()
        # What the installer does: a plain copy of the OLDER commit's files,
        # no .git, plus a venv that must stay ignored and unstaged.
        (install / "run.py").write_text("first\n", encoding="utf-8")
        (install / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        (install / ".venv").mkdir()
        (install / ".venv" / "junk").write_text("x", encoding="utf-8")

        monkeypatch.setattr(upd, "_REPO_ROOT", install)
        monkeypatch.setattr(upd, "_GITHUB_REPO_URL", str(src).removesuffix(".git"))

        result = asyncio.run(upd.link_checkout())

        assert result["linked"] is True
        assert result["sha"] == first_sha
        assert self._git(install, "rev-parse", "HEAD").stdout.strip() == first_sha
        assert (install / "run.py").read_text(encoding="utf-8") == "first\n"
        assert self._git(install, "status", "--porcelain").stdout.strip() == ""

    def test_the_update_check_then_sees_the_one_commit_it_is_behind(
        self, origin, tmp_path, monkeypatch, git_exists,
    ):
        """Linking is only worth anything if the normal update path works
        afterwards -- this is the state the Update page and the header badge
        read, and the admin console's Outdated badge with it."""
        src, first_sha = origin
        install = tmp_path / "install"
        install.mkdir()
        (install / "run.py").write_text("first\n", encoding="utf-8")
        (install / ".gitignore").write_text(".venv/\n", encoding="utf-8")
        monkeypatch.setattr(upd, "_REPO_ROOT", install)
        monkeypatch.setattr(upd, "_GITHUB_REPO_URL", str(src).removesuffix(".git"))

        asyncio.run(upd.link_checkout())
        state = asyncio.run(upd.check_for_update())

        assert state["available"] is True
        assert state["local_sha"] == first_sha
        assert [c["summary"] for c in state["commits"]] == ["two"]
