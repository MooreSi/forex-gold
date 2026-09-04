"""Getting a new EA onto a remote machine without anyone touching it.

The repo copy of `mql5/ForexTraderBridge.mq5` and the terminal's compiled
`.ex5` are two unlinked files, and nothing in MetaTrader says the build it is
running predates the source. `tools/deploy_ea.sh` closes that gap by hand, on
macOS, for whoever remembers to run it. Remote users have neither the shell
script nor the habit, so every EA change has so far needed a human at each
terminal.

What this module automates, and what it deliberately does not:

  copy      YES -- every MQL5/Experts folder on this machine, Windows and
            macOS/CrossOver, verified byte-identical after writing.
  compile   WINDOWS ONLY. metaeditor64.exe /compile works headlessly there.
            It does NOT work under CrossOver: `tools/deploy_ea.sh`'s own
            header records that it exits 0, writes no log and rebuilds
            nothing. So on macOS this refuses rather than pretending, and the
            portable answer is to ship a PRE-COMPILED .ex5 in the repo -- then
            no remote machine needs a compiler at all.
  attach    NO. Attaching an EA to a chart is not scriptable at runtime; it
            needs a chart template at terminal start. Reloading an ALREADY
            attached EA when its .ex5 changes is the case that matters, and
            the terminal does that itself.

Nothing here talks to MT5, places an order, or runs MetaEditor: every test
builds a fake terminal tree under tmp_path, and the compile tests assert that
no subprocess is spawned.
"""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from backend.src.services.broker import ea_deploy


EA = "ForexTraderBridge"


def _repo(tmp_path: Path, source: str = "// v1.06\n", binary: bytes | None = None) -> Path:
    """A stand-in checkout: mql5/ForexTraderBridge.mq5, optionally a .ex5."""
    mql5 = tmp_path / "repo" / "mql5"
    mql5.mkdir(parents=True)
    (mql5 / f"{EA}.mq5").write_text(source, encoding="utf-8")
    if binary is not None:
        (mql5 / f"{EA}.ex5").write_bytes(binary)
    return tmp_path / "repo"


def _windows_terminal(home: Path, term_id: str = "D0E8209F77C8CF37AD8BF550E51FF075") -> Path:
    d = home / "AppData" / "Roaming" / "MetaQuotes" / "Terminal" / term_id / "MQL5" / "Experts"
    d.mkdir(parents=True)
    return d


def _crossover_terminal(home: Path, bottle: str = "MetaTrader 5") -> Path:
    d = (home / "Library" / "Application Support" / "CrossOver" / "Bottles" / bottle
         / "drive_c" / "users" / "crossover" / "AppData" / "Roaming" / "MetaQuotes"
         / "Terminal" / "ABC123" / "MQL5" / "Experts")
    d.mkdir(parents=True)
    return d


def _sha(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ── Finding the terminals ─────────────────────────────────────────────────────

class TestDiscovery:

    def test_it_finds_a_windows_roaming_terminal(self, tmp_path):
        home = tmp_path / "home"
        expected = _windows_terminal(home)

        assert ea_deploy.experts_dirs(home=home) == [expected]

    def test_it_finds_a_crossover_bottle(self, tmp_path):
        home = tmp_path / "home"
        expected = _crossover_terminal(home)

        assert ea_deploy.experts_dirs(home=home) == [expected]

    def test_it_finds_every_terminal_on_the_machine(self, tmp_path):
        """Several installs per machine is the norm -- a live one, a demo one,
        and whichever the user actually has open changes."""
        home = tmp_path / "home"
        a = _windows_terminal(home, "AAA")
        b = _windows_terminal(home, "BBB")
        c = _crossover_terminal(home)

        assert sorted(ea_deploy.experts_dirs(home=home)) == sorted([a, b, c])

    def test_a_machine_with_no_metatrader_finds_nothing(self, tmp_path):
        assert ea_deploy.experts_dirs(home=tmp_path / "home") == []

    def test_it_does_not_invent_directories(self, tmp_path):
        """Discovery is read-only. A deploy that creates an Experts folder has
        found a terminal that does not exist."""
        home = tmp_path / "home"
        home.mkdir()

        ea_deploy.experts_dirs(home=home)

        assert list(home.rglob("Experts")) == []


# ── Copying ───────────────────────────────────────────────────────────────────

class TestDeployingTheSource:

    def test_it_writes_the_repo_source_into_the_terminal(self, tmp_path):
        home = tmp_path / "home"
        target = _windows_terminal(home)
        repo = _repo(tmp_path, source="// v1.06 harvest is a basket total\n")

        ea_deploy.deploy(repo_root=repo, home=home)

        assert (target / f"{EA}.mq5").read_text() == "// v1.06 harvest is a basket total\n"

    def test_the_copy_is_verified_byte_identical(self, tmp_path):
        home = tmp_path / "home"
        target = _windows_terminal(home)
        repo = _repo(tmp_path)

        report = ea_deploy.deploy(repo_root=repo, home=home)

        assert _sha(target / f"{EA}.mq5") == _sha(repo / "mql5" / f"{EA}.mq5")
        assert report["deployed"] == 1

    def test_an_already_current_target_is_left_alone(self, tmp_path):
        """Idempotent: the update path runs this on every pull, and rewriting
        an identical file would reset its mtime and make the .ex5 beside it
        look stale on every run."""
        home = tmp_path / "home"
        target = _windows_terminal(home)
        repo = _repo(tmp_path)
        ea_deploy.deploy(repo_root=repo, home=home)

        report = ea_deploy.deploy(repo_root=repo, home=home)

        assert report["deployed"] == 0
        assert report["already_current"] == 1

    def test_the_file_it_replaces_is_kept(self, tmp_path):
        """The terminal's copy is not under version control, so the backup is
        its only remaining trace."""
        home = tmp_path / "home"
        target = _windows_terminal(home)
        (target / f"{EA}.mq5").write_text("// the build that was running\n", encoding="utf-8")
        repo = _repo(tmp_path)

        ea_deploy.deploy(repo_root=repo, home=home)

        backups = list(target.glob(f"{EA}.mq5.bak-*"))
        assert len(backups) == 1
        assert backups[0].read_text() == "// the build that was running\n"

    def test_every_terminal_gets_it(self, tmp_path):
        home = tmp_path / "home"
        a = _windows_terminal(home, "AAA")
        b = _crossover_terminal(home)
        repo = _repo(tmp_path)

        ea_deploy.deploy(repo_root=repo, home=home)

        assert (a / f"{EA}.mq5").exists() and (b / f"{EA}.mq5").exists()

    def test_a_missing_repo_source_deploys_nothing(self, tmp_path):
        """A packaged install ships the .ex5 without the .mq5. Nothing to copy
        is not an error, and must not blank a working terminal's copy."""
        home = tmp_path / "home"
        target = _windows_terminal(home)
        (target / f"{EA}.mq5").write_text("// still here\n", encoding="utf-8")
        empty = tmp_path / "norepo"
        (empty / "mql5").mkdir(parents=True)

        report = ea_deploy.deploy(repo_root=empty, home=home)

        assert (target / f"{EA}.mq5").read_text() == "// still here\n"
        assert report["deployed"] == 0


class TestDeployingTheCompiledBinary:
    """The portable answer: compile once, ship the .ex5, and no remote machine
    needs MetaEditor at all."""

    def test_the_ex5_is_copied_when_the_repo_carries_one(self, tmp_path):
        home = tmp_path / "home"
        target = _windows_terminal(home)
        repo = _repo(tmp_path, binary=b"MQ5\x00compiled-1.06")

        ea_deploy.deploy(repo_root=repo, home=home)

        assert (target / f"{EA}.ex5").read_bytes() == b"MQ5\x00compiled-1.06"

    def test_a_repo_with_no_ex5_leaves_the_terminals_binary_alone(self, tmp_path):
        """Deleting a working compiled EA because the repo has none would take
        the bridge down on every remote machine at once."""
        home = tmp_path / "home"
        target = _windows_terminal(home)
        (target / f"{EA}.ex5").write_bytes(b"locally-compiled")
        repo = _repo(tmp_path)

        ea_deploy.deploy(repo_root=repo, home=home)

        assert (target / f"{EA}.ex5").read_bytes() == b"locally-compiled"

    def test_a_shipped_binary_means_no_compile_is_needed(self, tmp_path):
        home = tmp_path / "home"
        _windows_terminal(home)
        repo = _repo(tmp_path, binary=b"compiled")

        report = ea_deploy.deploy(repo_root=repo, home=home)

        assert report["needs_compile"] == 0

    def test_a_terminal_with_no_binary_is_reported_as_needing_one(self, tmp_path):
        home = tmp_path / "home"
        _windows_terminal(home)
        repo = _repo(tmp_path)

        report = ea_deploy.deploy(repo_root=repo, home=home)

        assert report["needs_compile"] == 1

    def test_a_binary_older_than_its_source_is_reported_too(self, tmp_path):
        """The exact condition that hid the original problem: an .ex5 sitting
        beside a newer .mq5 is a build nobody compiled."""
        import os
        home = tmp_path / "home"
        target = _windows_terminal(home)
        repo = _repo(tmp_path)
        ea_deploy.deploy(repo_root=repo, home=home)
        (target / f"{EA}.ex5").write_bytes(b"old")
        os.utime(target / f"{EA}.ex5", (1_000_000, 1_000_000))

        report = ea_deploy.deploy(repo_root=repo, home=home)

        assert report["needs_compile"] == 1


# ── Failure ───────────────────────────────────────────────────────────────────

class TestItNeverTakesTheUpdateDown:
    """This runs inside apply_update, after the pull has already succeeded.
    Nothing here may raise: a failed EA copy must not turn a good app update
    into a failed one."""

    def test_one_unwritable_target_does_not_stop_the_others(self, tmp_path):
        home = tmp_path / "home"
        bad = _windows_terminal(home, "AAA")
        good = _windows_terminal(home, "BBB")
        repo = _repo(tmp_path)
        bad.chmod(0o500)
        try:
            report = ea_deploy.deploy(repo_root=repo, home=home)
        finally:
            bad.chmod(0o700)

        assert (good / f"{EA}.mq5").exists()
        assert report["errors"]

    def test_deploy_after_update_swallows_everything(self, tmp_path, monkeypatch):
        def _boom(**kw):
            raise RuntimeError("filesystem gone")
        monkeypatch.setattr(ea_deploy, "deploy", _boom)

        assert ea_deploy.deploy_after_update()["ok"] is False

    def test_deploy_after_update_reports_success_normally(self, tmp_path):
        """Negative control: the swallow above must not be hiding a function
        that never works."""
        home = tmp_path / "home"
        _windows_terminal(home)
        repo = _repo(tmp_path)

        assert ea_deploy.deploy_after_update(repo_root=repo, home=home)["ok"] is True


# ── Compiling ─────────────────────────────────────────────────────────────────

class TestCompiling:

    def test_it_refuses_on_macos_instead_of_pretending(self, tmp_path):
        """MetaEditor's /compile under CrossOver exits 0, writes no log and
        rebuilds nothing -- the failure mode that cost a day. Reporting "not
        supported here" is the only honest answer."""
        result = ea_deploy.compile_ea(tmp_path, platform="darwin")

        assert result["ok"] is False
        assert "crossover" in result["detail"].lower()

    def test_it_spawns_nothing_on_macos(self, tmp_path, monkeypatch):
        spawned = []
        monkeypatch.setattr(ea_deploy.subprocess, "run",
                            lambda *a, **k: spawned.append(a))

        ea_deploy.compile_ea(tmp_path, platform="darwin")

        assert spawned == []

    def test_it_reports_a_missing_metaeditor_on_windows(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ea_deploy, "_metaeditor_path", lambda: None)

        result = ea_deploy.compile_ea(tmp_path, platform="win32")

        assert result["ok"] is False
        assert "metaeditor" in result["detail"].lower()

    def test_it_runs_metaeditor_on_windows(self, tmp_path, monkeypatch):
        """The one path that does spawn. Asserted on the argv it builds, since
        a wrong flag here fails silently -- MetaEditor exits 0 either way."""
        editor = tmp_path / "metaeditor64.exe"
        editor.write_text("")
        src = tmp_path / f"{EA}.mq5"
        src.write_text("// ea\n")
        calls = []

        class _Done:
            returncode = 0
            stdout = ""
        monkeypatch.setattr(ea_deploy, "_metaeditor_path", lambda: editor)
        monkeypatch.setattr(ea_deploy.subprocess, "run",
                            lambda args, **kw: calls.append(args) or _Done())

        ea_deploy.compile_ea(tmp_path, platform="win32")

        assert calls and str(editor) == calls[0][0]
        assert any(a.startswith("/compile:") and a.endswith(f"{EA}.mq5") for a in calls[0])
