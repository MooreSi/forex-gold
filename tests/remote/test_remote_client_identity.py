"""This machine's identity to the licence server, and what it sends back.

Two things live here that nothing else can repair if they go wrong:

  * The token in client_token.txt IS this machine to the admin server. It is
    what a licence is issued against. Regenerate it and the client loses its
    licence and reappears as an unapproved pending registration -- with the old
    entry still sitting in the owner's approved list under a token nothing
    holds any more.
  * _build_diagnostics() ships log lines to the admin server on request. What
    it keeps is a filter, and a filter that silently drops the wrong level is
    the exact bug already fixed here once: the original [24:31] slice worked
    for WARNING (7 chars) and silently dropped INFO (4) and ERROR (5), so
    diagnostics arrived containing no errors at all.

Every path is redirected into tmp_path. No socket, and no reading of the real
48 MB application log.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.remote import client as rc


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    d = tmp_path / "remote"
    monkeypatch.setattr(rc, "_REMOTE_DIR", d)
    monkeypatch.setattr(rc, "_TOKEN_FILE", d / "client_token.txt")
    monkeypatch.setattr(rc, "_EMAIL_FILE", d / "client_email.txt")
    monkeypatch.setattr(rc, "_NICKNAME_FILE", d / "client_nickname.txt")
    monkeypatch.setattr(rc, "_client_task", None)
    return d


class TestTheMachineToken:
    def test_it_creates_one_on_first_run(self, isolated):
        tok = rc.get_or_create_token()
        assert len(tok) == 64
        assert (isolated / "client_token.txt").read_text().strip() == tok

    def test_it_is_hex(self):
        int(rc.get_or_create_token(), 16)          # raises if it is not

    def test_IT_IS_STABLE_ACROSS_CALLS(self):
        """The whole point. A token that changed would lose the licence issued
        against it and re-appear as an unapproved registration."""
        assert rc.get_or_create_token() == rc.get_or_create_token()

    def test_it_is_read_back_from_disk_not_regenerated(self, isolated):
        (isolated).mkdir(parents=True, exist_ok=True)
        (isolated / "client_token.txt").write_text("a" * 64)
        assert rc.get_or_create_token() == "a" * 64

    def test_surrounding_whitespace_is_stripped(self, isolated):
        """A file edited by hand, or written with a trailing newline, must not
        read as a different token than the one that was issued."""
        isolated.mkdir(parents=True, exist_ok=True)
        (isolated / "client_token.txt").write_text("  " + "b" * 64 + "\n")
        assert rc.get_or_create_token() == "b" * 64

    @pytest.mark.parametrize("bad", ["", "short", "c" * 63, "c" * 65])
    def test_a_WRONG_LENGTH_token_is_replaced(self, isolated, bad):
        """A truncated or empty file is not an identity. Trusting it would
        send a token the server can never match."""
        isolated.mkdir(parents=True, exist_ok=True)
        (isolated / "client_token.txt").write_text(bad)

        tok = rc.get_or_create_token()

        assert len(tok) == 64 and tok != bad
        assert (isolated / "client_token.txt").read_text() == tok

    def test_two_machines_do_not_share_a_token(self, isolated, tmp_path, monkeypatch):
        first = rc.get_or_create_token()

        other = tmp_path / "other"
        monkeypatch.setattr(rc, "_REMOTE_DIR", other)
        monkeypatch.setattr(rc, "_TOKEN_FILE", other / "client_token.txt")

        assert rc.get_or_create_token() != first


class TestStoredRegistrationDetails:
    def test_they_are_empty_before_registering(self):
        assert rc.get_stored_email() == ""
        assert rc.get_stored_nickname() == ""

    def test_registration_stores_them(self):
        rc.request_registration("simon@example.com", "Simon")
        assert rc.get_stored_email() == "simon@example.com"
        assert rc.get_stored_nickname() == "Simon"

    def test_they_are_stripped_ON_THE_WAY_IN(self, isolated):
        """Asserted against the FILE, not the getter. Both getters strip on
        read as well, so comparing what comes back out passes whether or not
        the write-side strip exists -- found by mutation, which survived
        removing it. The file is what a person reads when debugging a
        registration that the server rejected."""
        rc.request_registration("  simon@example.com \n", "  Simon  ")

        assert (isolated / "client_email.txt").read_text() == "simon@example.com"
        assert (isolated / "client_nickname.txt").read_text() == "Simon"
        assert rc.get_stored_email() == "simon@example.com"
        assert rc.get_stored_nickname() == "Simon"

    def test_a_BLANK_nickname_does_not_erase_a_stored_one(self):
        """Deliberate asymmetry: the nickname is only written when non-empty,
        so re-registering with the field left blank keeps the existing one.
        The email is written unconditionally."""
        rc.request_registration("simon@example.com", "Simon")

        rc.request_registration("new@example.com", "")

        assert rc.get_stored_nickname() == "Simon"
        assert rc.get_stored_email() == "new@example.com"

    def test_it_resets_the_delivery_timestamp(self):
        """Each request is judged on its own delivery. Leaving the old value
        would make an undelivered request look like it had arrived."""
        rc._status["registration_sent_at"] = 12345.0

        rc.request_registration("simon@example.com")

        assert rc._status["registration_sent_at"] == 0.0

    def test_it_works_with_no_event_loop_running(self):
        """Called from the NiceGUI settings page. The loop restart is wrapped
        in try/except RuntimeError precisely so the details are still saved
        when there is no running loop."""
        rc.request_registration("simon@example.com", "Simon")   # must not raise
        assert rc.get_stored_email() == "simon@example.com"


class TestStatusIsACopy:
    def test_get_status_returns_a_copy(self):
        """Read by the settings page every render. Handing out the live dict
        lets a caller flip `is_remote_admin` on the real status."""
        snapshot = rc.get_status()
        snapshot["is_remote_admin"] = True
        assert rc.get_status()["is_remote_admin"] is not True


class TestLogLevelExtraction:
    """A regression test for a bug that already happened: the original
    [24:31] slice was the width of WARNING, so INFO and ERROR were dropped and
    diagnostics arrived showing no errors."""

    @pytest.mark.parametrize("level", ["INFO", "ERROR", "WARNING", "DEBUG", "CRITICAL"])
    def test_every_level_is_extracted_whatever_its_length(self, level):
        line = f"2026-08-28 17:49:59,444 {level} backend.src.something — a message"
        assert rc._log_level(line) == level

    def test_a_short_line_yields_nothing_rather_than_raising(self):
        assert rc._log_level("2026-08-28") == ""

    def test_a_continuation_line_is_not_mistaken_for_a_level(self):
        """Tracebacks are indented continuations of the previous record. They
        carry no level and must not be read as one."""
        assert rc._log_level("    File \"x.py\", line 1, in <module>") not in (
            "ERROR", "WARNING", "INFO")


class TestTheDiagnosticsFilter:
    @pytest.fixture
    def log_dir(self, monkeypatch, tmp_path):
        from backend.src import config as cfg
        monkeypatch.setattr(cfg, "DATA_DIR", str(tmp_path))
        return tmp_path

    def _write(self, log_dir, lines):
        (log_dir / "forex_trader.log").write_text("\n".join(lines), encoding="utf-8")

    def _stamp(self):
        from datetime import datetime
        return datetime.now().strftime("%Y-%m-%d %H:%M:%S,000")

    def test_errors_and_warnings_are_kept(self, log_dir):
        ts = self._stamp()
        self._write(log_dir, [f"{ts} ERROR mod — it broke",
                              f"{ts} WARNING mod — it complained"])

        lines = rc._build_diagnostics()["data"]["log_lines"]

        assert any("it broke" in l for l in lines)
        assert any("it complained" in l for l in lines)

    def test_polling_noise_is_dropped(self, log_dir):
        """The bridge polls several times a second. Unfiltered, 500 lines of
        diagnostics would be entirely GET /tick/."""
        ts = self._stamp()
        self._write(log_dir, [f"{ts} INFO httpx — GET /tick/XAUUSD 200 OK",
                              f"{ts} INFO mod — signal parsed"])

        lines = rc._build_diagnostics()["data"]["log_lines"]

        assert not any("/tick/" in l for l in lines)
        assert any("signal parsed" in l for l in lines)

    def test_a_NOISY_ERROR_is_still_kept(self, log_dir):
        """Only INFO is filtered for noise. An error mentioning /order is an
        order that failed, which is exactly what diagnostics are for."""
        ts = self._stamp()
        self._write(log_dir, [f"{ts} ERROR bridge — POST /order failed: no connection"])

        lines = rc._build_diagnostics()["data"]["log_lines"]

        assert any("POST /order failed" in l for l in lines)

    def test_chronological_order_is_preserved(self, log_dir):
        """It is collected by walking backwards. Reporting it that way would
        show every diagnostic newest-first, reading as a different fault."""
        from datetime import datetime
        d = datetime.now().strftime("%Y-%m-%d")
        self._write(log_dir, [f"{d} 09:00:00,000 ERROR mod — first",
                              f"{d} 09:00:01,000 ERROR mod — second"])

        lines = rc._build_diagnostics()["data"]["log_lines"]

        assert [l.split("— ")[1] for l in lines] == ["first", "second"]

    def test_a_missing_log_file_is_not_fatal(self, log_dir):
        data = rc._build_diagnostics()["data"]
        assert data["log_lines"] == []
        assert data["hostname"]

    def test_it_reports_the_machine_and_version_even_with_no_log(self, log_dir):
        data = rc._build_diagnostics()["data"]
        assert data["platform"]
        assert data["python"]
        assert isinstance(data["uptime_s"], int)
