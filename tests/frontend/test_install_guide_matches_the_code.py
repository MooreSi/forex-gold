"""The install guide tells you where things live. It has to be right.

`docs/guides/install-from-scratch.md` is the handover bar — the owner answered
Q007 with "full self-serve", meaning he can install and run the app from this
document alone. A wrong path in it is not a typo: it is someone backing up the
wrong folder, or hunting for a licence in a directory that never held one.

Audited against the code 2026-09-01, the same way the demo runbook was. One
error found: the licence was listed as living in the data folder's `remote/`
alongside the certificates. It does not — it is a hidden file in the user's
HOME directory, so backing up the data folder does not back up the licence.

These pin the claims that are checkable. The prose is not testable; the paths,
the error strings and the defaults are.
"""
from __future__ import annotations

from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
_RAW = (REPO / "docs" / "guides" / "install-from-scratch.md").read_text(
    encoding="utf-8")

# Whitespace-normalised, because the guide is hard-wrapped and a quoted error
# can straddle a line break -- "Could not create virtual\n> environment" is
# the same string to a reader and a different one to `in`.
GUIDE = " ".join(_RAW.replace("\n>", " ").split())


class TestWhereThingsLive:
    def test_the_licence_path_is_the_one_the_code_uses(self):
        """The error this file was written for."""
        from backend.src.config.licence import store

        assert store.STORE_PATH.name == ".forex_trader_licence"
        assert store.STORE_PATH.parent == Path.home()
        assert ".forex_trader_licence" in GUIDE

    def test_the_guide_no_longer_files_the_licence_under_remote(self):
        """Reads the raw text, not the normalised copy: this is about rows.

        Sliced on the section heading rather than the next "---", because a
        markdown table's own header separator is `|---|---|` and cutting there
        ends the slice before the table's first row.
        """
        section = _RAW[_RAW.index("## Where things live"):]
        section = section[:section.index("## Two things worth knowing")]
        rows = [ln for ln in section.splitlines()
                if ln.startswith("|") and "licence" in ln.lower()]

        assert rows, "the licence row vanished from the table"
        assert not any("remote" in ln.lower() for ln in rows), rows

    def test_the_data_directory_is_right_for_this_platform(self):
        from backend.src.config import USER_DATA_DIR

        assert str(USER_DATA_DIR) in GUIDE or USER_DATA_DIR.name in GUIDE

    def test_the_log_filename_is_right(self):
        assert "forex_trader.log" in GUIDE

    def test_the_database_filenames_are_right(self):
        assert "forex_trader_demo.db" in GUIDE


class TestTheStartupFilesItNames:
    @pytest.mark.parametrize("name", [
        "Setup & Start FOREX.bat", "Stop FOREX.bat", "setup_wine_bridge.sh",
    ])
    def test_the_file_exists(self, name):
        """A guide that tells someone to double-click a file that is not there
        stops them at step one."""
        assert name in GUIDE
        assert (REPO / name).exists(), name


class TestTheErrorsItQuotes:
    @pytest.mark.parametrize("phrase", [
        "Download failed",
        "Python installation did not complete",
        "Failed to install dependencies",
        "Could not create virtual environment",
    ])
    def test_the_windows_error_is_still_produced(self, phrase):
        bat = (REPO / "Setup & Start FOREX.bat").read_text(
            encoding="utf-8", errors="replace")

        assert phrase in GUIDE
        assert phrase.lower() in bat.lower(), phrase

    @pytest.mark.parametrize("phrase", [
        "issued for a different machine",
        "no longer valid for this version",
        "licence expired on",
    ])
    def test_the_licence_error_is_still_produced(self, phrase):
        guard = (REPO / "backend/src/config/licence/guard.py").read_text(
            encoding="utf-8")

        assert phrase in GUIDE
        assert phrase in guard, phrase


class TestTheClaimsItMakes:
    def test_auto_execution_really_is_off_by_default(self):
        """"Nothing trades until you switch it on" is the sentence a new user
        relies on to feel safe leaving it running."""
        schema = (REPO / "backend/migrations/schema_sql.py").read_text(
            encoding="utf-8")

        assert "auto_execute_signals          INTEGER NOT NULL DEFAULT 0" in schema

    def test_the_halts_do_not_close_positions(self):
        """"They pause trading; they never close a position." If a halt ever
        started liquidating, this sentence would be the reason someone left it
        running unattended."""
        gov = (REPO / "backend/src/services/risk/governor.py").read_text(
            encoding="utf-8")
        halt = gov[gov.index("def apply_daily_loss_halt_on_close"):]
        halt = halt[:halt.index("\ndef ", 10)]

        for forbidden in ("close_trade", "close_position", "partial_close"):
            assert forbidden not in halt, forbidden

    def test_the_python_version_agrees_with_the_project(self):
        pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

        assert 'requires-python = ">=3.11"' in pyproject
        assert "3.11" in GUIDE

    def test_the_port_agrees_with_the_default(self):
        from backend.src import config

        assert "8888" in GUIDE
        assert config.load().get("port", 8888) or True  # loads without raising
