"""The bridge finds the Windows MetaTrader terminal and logs in to it.

Reported 2026-09-26: on the Windows VPS the EA showed connected but the bridge
never did. Every attempt logged `mt5.initialize() failed: (1, 'Success')`.
Diagnosed on the VPS over WinRM, read-only, then with one setting:

  * no terminal path was saved, so the bridge only ever tried to attach
    without one, which fails for the Vantage build in
    C:\\Program Files\\Vantage Markets MT5 Terminal -- and the Settings field
    said "Blank means auto-detect" while nothing detected anything;
  * with the path saved, the error became (-6, 'Terminal: Authorization
    failed'): initialize(path=...) was called without the account, and that
    terminal refuses the connection without one. MetaTrader's own API takes
    login/password/server in that call.

Nothing here reaches MetaTrader or a broker: `mt5` is a recorder, and the
terminal "installs" are folders under tmp_path.
"""
from __future__ import annotations

from pathlib import Path

import pytest

import mt5_terminal


class _Mt5:
    """MetaTrader5's module surface, recording every initialize call."""

    def __init__(self, attach_ok=False, path_ok=True):
        self.attach_ok, self.path_ok = attach_ok, path_ok
        self.calls: list[dict] = []

    def initialize(self, **kw):
        self.calls.append(kw)
        return self.attach_ok if "path" not in kw else self.path_ok

    def shutdown(self):
        pass

    def last_error(self):
        return (-6, "Terminal: Authorization failed")


class _Log:
    def info(self, *a):
        pass

    warning = error = info


ACCOUNT = dict(login=123456, password="pw", server="VantageMarkets-Demo")


def _install(appdata: Path, term_id: str, install: Path, encoding="utf-16") -> Path:
    install.mkdir(parents=True, exist_ok=True)
    exe = install / "terminal64.exe"
    exe.write_text("")
    data = appdata / "MetaQuotes" / "Terminal" / term_id
    data.mkdir(parents=True)
    (data / "origin.txt").write_text(str(install), encoding=encoding)
    return exe


def _connect(mt5, terminal_path="", appdata=None):
    return mt5_terminal.connect(mt5, terminal_path=terminal_path, timeout_ms=1000,
                                log=_Log(), appdata=appdata, **ACCOUNT)


class TestAttachingFirst:
    def test_a_running_terminal_is_attached_to_without_a_path(self, tmp_path):
        mt5 = _Mt5(attach_ok=True)

        ok, _ = _connect(mt5, terminal_path=r"C:\x\terminal64.exe")

        assert ok and mt5.calls == [{"timeout": 1000}]


class TestUsingAPath:
    def test_a_saved_path_is_used_with_the_account(self, tmp_path):
        """The (-6, Authorization failed) fix: the account goes in the same
        call that starts or attaches to the terminal."""
        mt5 = _Mt5()

        ok, _ = _connect(mt5, terminal_path=r"C:\Vantage\terminal64.exe")

        assert ok
        assert mt5.calls[-1] == {"path": r"C:\Vantage\terminal64.exe", "timeout": 1000, **ACCOUNT}

    def test_with_nothing_saved_the_one_installed_terminal_is_found(self, tmp_path):
        exe = _install(tmp_path, "725B", tmp_path / "Vantage Markets MT5 Terminal")
        mt5 = _Mt5()

        ok, _ = _connect(mt5, appdata=tmp_path)

        assert ok and mt5.calls[-1]["path"] == str(exe)

    def test_it_will_not_guess_between_two_installs(self, tmp_path):
        """Starting the wrong one would open a second terminal on another
        account. The operator's saved path is the answer there."""
        _install(tmp_path, "AAAA", tmp_path / "Vantage MT5")
        _install(tmp_path, "BBBB", tmp_path / "Other MT5")
        mt5 = _Mt5()

        ok, why = _connect(mt5, appdata=tmp_path)

        assert not ok
        assert all("path" not in c for c in mt5.calls)
        assert "terminal path" in why.lower()

    def test_a_refusal_is_reported_in_metatraders_words(self, tmp_path):
        mt5 = _Mt5(path_ok=False)

        ok, why = _connect(mt5, terminal_path=r"C:\Vantage\terminal64.exe")

        assert not ok and "Authorization failed" in why


class TestFindingTheTerminal:
    def test_it_reads_origin_txt_as_metatrader_writes_it(self, tmp_path):
        exe = _install(tmp_path, "725B", tmp_path / "Vantage Markets MT5 Terminal")

        assert mt5_terminal.find_terminal_path(appdata=tmp_path) == str(exe)

    def test_a_plain_utf8_origin_is_read_too(self, tmp_path):
        exe = _install(tmp_path, "725B", tmp_path / "MT5", encoding="utf-8")

        assert mt5_terminal.find_terminal_path(appdata=tmp_path) == str(exe)

    def test_a_data_folder_whose_install_is_gone_is_ignored(self, tmp_path):
        exe = _install(tmp_path, "LIVE", tmp_path / "Vantage MT5")
        stale = tmp_path / "MetaQuotes" / "Terminal" / "OLD"
        stale.mkdir(parents=True)
        (stale / "origin.txt").write_text(str(tmp_path / "uninstalled"), encoding="utf-16")

        assert mt5_terminal.find_terminal_path(appdata=tmp_path) == str(exe)

    def test_two_data_folders_for_one_install_count_once(self, tmp_path):
        install = tmp_path / "Vantage MT5"
        exe = _install(tmp_path, "AAAA", install)
        data = tmp_path / "MetaQuotes" / "Terminal" / "BBBB"
        data.mkdir(parents=True)
        (data / "origin.txt").write_text(str(install), encoding="utf-16")

        assert mt5_terminal.find_terminal_path(appdata=tmp_path) == str(exe)

    def test_no_metatrader_finds_nothing(self, tmp_path):
        assert mt5_terminal.find_terminal_path(appdata=tmp_path) == ""


def test_the_bridge_connects_through_it():
    """The bridge's own copy of this sequence is gone, not left beside it."""
    src = (Path(__file__).resolve().parents[2] / "mt5_bridge.py").read_text(encoding="utf-8")
    assert "mt5_terminal.connect(" in src
    assert "No running MT5 found; launching via path" not in src
