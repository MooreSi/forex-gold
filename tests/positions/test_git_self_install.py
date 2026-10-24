"""A Windows install with no git gets one from the app itself.

Both of our Windows routes put PortableGit in %LOCALAPPDATA%\\Programs\\PortableGit
(the installer always, the .bat when git is missing). A third-party repackaged
zip ("FOREX_Trader_WindowsServer_含环境_20261018", its own .runtime\\Python313)
ran neither, so the machine had no git, no .git, and every admin-console push
failed with "git not found on PATH" (2026-10-23, 10_0_0_3 and EV-156MMIO7U6F).
We do not build that zip, so the app has to make sure of git itself. Spec 027.
"""
from __future__ import annotations

import asyncio
import hashlib
import re
from pathlib import Path

import pytest

from backend.src.services.positions import core_app_update as upd
from backend.src.services.positions import core_git_exe
from backend.src.services.positions import core_git_install as gi

REPO = Path(__file__).resolve().parents[2]
FAKE_ARCHIVE = b"not really a 7z self-extractor"


@pytest.fixture
def windows_without_git(monkeypatch, tmp_path):
    monkeypatch.setattr(gi, "_is_windows", lambda: True)
    monkeypatch.setattr(core_git_exe.shutil, "which", lambda _n: None)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setattr(gi, "PORTABLE_GIT_SHA256", hashlib.sha256(FAKE_ARCHIVE).hexdigest())
    return tmp_path


def _serve(monkeypatch, payload=FAKE_ARCHIVE, seen=None):
    def _download(url, dest):
        if seen is not None:
            seen.append(url)
        Path(dest).write_bytes(payload)
    monkeypatch.setattr(gi, "_download", _download)


def _extracts_git(monkeypatch, ran=None):
    def _extract(archive, target):
        if ran is not None:
            ran.append((Path(archive), Path(target)))
        exe = Path(target) / "cmd" / "git.exe"
        exe.parent.mkdir(parents=True, exist_ok=True)
        exe.write_text("", encoding="utf-8")
    monkeypatch.setattr(gi, "_extract", _extract)


class TestWhenItDoesNothing:
    def test_not_on_windows(self, monkeypatch):
        monkeypatch.setattr(gi, "_is_windows", lambda: False)
        monkeypatch.setattr(gi, "_download", lambda *_a: pytest.fail("downloaded off Windows"))

        assert gi.ensure_git() == {"installed": False, "reason": "not-windows"}

    def test_git_is_already_there(self, monkeypatch, windows_without_git):
        monkeypatch.setattr(core_git_exe.shutil, "which", lambda _n: r"C:\Git\cmd\git.exe")
        monkeypatch.setattr(gi, "_download", lambda *_a: pytest.fail("downloaded with git present"))

        assert gi.ensure_git() == {"installed": False, "reason": "already-present"}

    def test_no_localappdata(self, monkeypatch, windows_without_git):
        monkeypatch.delenv("LOCALAPPDATA")
        monkeypatch.setattr(gi, "_download", lambda *_a: pytest.fail("downloaded with nowhere to put it"))

        assert gi.ensure_git() == {"installed": False, "reason": "no-localappdata"}


class TestInstalling:
    def test_a_good_download_lands_where_git_exe_looks(self, monkeypatch, windows_without_git):
        seen, ran = [], []
        _serve(monkeypatch, seen=seen)
        _extracts_git(monkeypatch, ran)

        assert gi.ensure_git() == {"installed": True, "reason": "installed"}
        assert seen == [gi.PORTABLE_GIT_URL]
        assert ran[0][1] == windows_without_git / "Programs" / "PortableGit"
        assert core_git_exe.git_exe() == str(
            windows_without_git / "Programs" / "PortableGit" / "cmd" / "git.exe")

    def test_the_download_is_removed_afterwards(self, monkeypatch, windows_without_git):
        ran = []
        _serve(monkeypatch)
        _extracts_git(monkeypatch, ran)

        gi.ensure_git()

        assert not ran[0][0].exists()

    def test_the_remembered_git_version_is_forgotten_so_the_heartbeat_sees_it(
        self, monkeypatch, windows_without_git,
    ):
        monkeypatch.setattr(upd, "_GIT_VERSION", "")
        _serve(monkeypatch)
        _extracts_git(monkeypatch)

        gi.ensure_git()

        assert upd._GIT_VERSION is None


class TestRefusing:
    def test_a_wrong_hash_is_never_run_and_is_deleted(self, monkeypatch, windows_without_git):
        _serve(monkeypatch, payload=b"tampered")
        monkeypatch.setattr(gi, "_extract", lambda *_a: pytest.fail("ran an unverified download"))

        assert gi.ensure_git() == {"installed": False, "reason": "hash-mismatch"}
        assert list((windows_without_git / "Programs").glob("*download*")) == []

    def test_a_failed_download_is_reported_not_raised(self, monkeypatch, windows_without_git):
        def _boom(url, dest):
            raise OSError("getaddrinfo failed")
        monkeypatch.setattr(gi, "_download", _boom)

        assert gi.ensure_git() == {"installed": False, "reason": "download-failed"}

    def test_an_extraction_that_leaves_no_git_exe_is_a_failure(self, monkeypatch, windows_without_git):
        _serve(monkeypatch)
        monkeypatch.setattr(gi, "_extract", lambda *_a: None)

        assert gi.ensure_git() == {"installed": False, "reason": "extract-failed"}

    def test_an_extraction_that_raises_is_a_failure(self, monkeypatch, windows_without_git):
        _serve(monkeypatch)

        def _boom(*_a):
            raise OSError("access denied")
        monkeypatch.setattr(gi, "_extract", _boom)

        assert gi.ensure_git() == {"installed": False, "reason": "extract-failed"}


class TestTheExtractor:
    def test_it_runs_the_self_extractor_silently_into_the_target(self, monkeypatch, tmp_path):
        seen = []

        class _Done:
            returncode = 0

        def _run(args, **kw):
            seen.append(args)
            return _Done()
        monkeypatch.setattr(gi.subprocess, "run", _run)

        gi._extract(tmp_path / "pg.7z.exe", tmp_path / "PortableGit")

        assert seen == [[str(tmp_path / "pg.7z.exe"), "-y", f"-o{tmp_path / 'PortableGit'}"]]

    def test_a_non_zero_exit_raises(self, monkeypatch, tmp_path):
        class _Failed:
            returncode = 2
        monkeypatch.setattr(gi.subprocess, "run", lambda *_a, **_k: _Failed())

        with pytest.raises(RuntimeError):
            gi._extract(tmp_path / "pg.7z.exe", tmp_path / "PortableGit")


class TestOnePinEverywhere:
    """The app, the .bat and the installer must fetch the same build, or a
    machine set up one way runs a different git from one set up another."""

    def test_the_hash_is_a_sha256(self):
        assert re.fullmatch(r"[0-9a-f]{64}", gi.PORTABLE_GIT_SHA256)

    def test_the_bat_fetches_the_same_build_and_checks_the_same_hash(self):
        bat = (REPO / "Setup & Start FOREX.bat").read_text(encoding="utf-8")
        assert gi.PORTABLE_GIT_URL in bat
        assert gi.PORTABLE_GIT_SHA256 in bat.lower()

    def test_the_installer_fetches_the_same_build(self):
        iss = (REPO / "installer" / "FOREX_Trader_Setup.iss").read_text(encoding="utf-8")
        assert gi.PORTABLE_GIT_URL in iss


class TestAtStartup:
    def test_git_is_ensured_before_the_install_tries_to_link(self, monkeypatch):
        from backend.src import app
        order = []
        monkeypatch.setattr(gi, "ensure_git", lambda: order.append("ensure") or
                            {"installed": False, "reason": "already-present"})

        async def _link():
            order.append("link")
            return {"linked": False, "sha": "", "reason": "already-linked"}
        monkeypatch.setattr(upd, "link_checkout", _link)

        asyncio.run(app._link_checkout_at_startup())

        assert order == ["ensure", "link"]

    def test_a_crash_in_ensure_git_still_lets_linking_run(self, monkeypatch):
        from backend.src import app
        order = []

        def _boom():
            raise RuntimeError("unexpected")
        monkeypatch.setattr(gi, "ensure_git", _boom)

        async def _link():
            order.append("link")
            return {"linked": False, "sha": "", "reason": "already-linked"}
        monkeypatch.setattr(upd, "link_checkout", _link)

        asyncio.run(app._link_checkout_at_startup())

        assert order == ["link"]
