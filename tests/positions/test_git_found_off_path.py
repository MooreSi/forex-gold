"""The updater must find the git the Windows installer put on the machine.

The installer downloads PortableGit into %LOCALAPPDATA%\\Programs\\PortableGit
and never puts it on the machine's PATH; only "Setup & Start FOREX.bat" adds it,
for the session it starts. An app restarted any other way (a shortcut, a
scheduled task, a restart that did not inherit that PATH) therefore saw no git,
`link_checkout()` answered "already-linked" for a broken leftover .git, and the
admin console said "commit unreadable" for good (2026-10-02, a client's machine).
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.positions import core_app_update as upd


@pytest.fixture
def no_path_git(monkeypatch):
    monkeypatch.setattr(upd.shutil, "which", lambda _n: None)


def _portable(tmp_path, sub="cmd"):
    exe = tmp_path / "Programs" / "PortableGit" / sub / "git.exe"
    exe.parent.mkdir(parents=True)
    exe.write_text("", encoding="utf-8")
    return exe


class TestFindingGit:
    def test_a_git_on_path_wins(self, monkeypatch, tmp_path):
        _portable(tmp_path)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        monkeypatch.setattr(upd.shutil, "which", lambda _n: "/usr/bin/git")

        assert upd._git_exe() == "/usr/bin/git"

    def test_the_installers_portable_git_is_used_when_path_has_none(
        self, monkeypatch, tmp_path, no_path_git,
    ):
        exe = _portable(tmp_path)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert upd._git_exe() == str(exe)

    def test_the_portable_bin_folder_is_found_too(self, monkeypatch, tmp_path, no_path_git):
        exe = _portable(tmp_path, "bin")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert upd._git_exe() == str(exe)

    def test_no_git_anywhere_is_none(self, monkeypatch, tmp_path, no_path_git):
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

        assert upd._git_exe() is None

    def test_without_localappdata_it_is_none(self, monkeypatch, no_path_git):
        monkeypatch.delenv("LOCALAPPDATA", raising=False)

        assert upd._git_exe() is None


class TestUsingIt:
    def test_git_commands_run_the_found_executable_not_a_bare_git(
        self, monkeypatch, tmp_path, no_path_git,
    ):
        exe = _portable(tmp_path)
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
        seen = []

        class _Done:
            returncode, stdout, stderr = 0, "abc\n", ""

        def _run(args, **_kw):
            seen.append(args)
            return _Done()

        monkeypatch.setattr(upd.subprocess, "run", _run)

        asyncio.run(upd._run_git("rev-parse", "HEAD"))

        assert seen == [[str(exe), "rev-parse", "HEAD"]]

    def test_a_broken_leftover_is_rebuilt_when_git_is_only_in_the_portable_folder(
        self, monkeypatch, tmp_path, no_path_git,
    ):
        _portable(tmp_path / "appdata")
        monkeypatch.setenv("LOCALAPPDATA", str(tmp_path / "appdata"))
        root = tmp_path / "install"
        (root / ".git").mkdir(parents=True)
        (root / ".git" / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        monkeypatch.setattr(upd, "_REPO_ROOT", root)
        calls = []

        async def _git(*args, **_kw):
            calls.append(args)
            return 1, "", "stopped here"

        monkeypatch.setattr(upd, "_run_git", _git)

        result = asyncio.run(upd.link_checkout())

        assert result["reason"] == "init-failed"
        assert calls == [("init",)]
