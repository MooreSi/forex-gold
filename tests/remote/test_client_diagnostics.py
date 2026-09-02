"""What leaves this machine when the admin asks for diagnostics.

`_build_diagnostics` packages the hostname, the platform, the build, and up to
**3,000 raw log lines** and ships them to the admin server. Q005 #1 established
from real captured logs that those lines used to carry the MT5 account number,
the broker server and the balance -- fixed at the source in `mt5_bridge.py`,
but the point stands: this function decides how much leaves the machine.

Two properties therefore matter more than the rest:

  * **The raw tail is bounded.** Unbounded, a long-running install ships a
    multi-hundred-megabyte log over a websocket to answer a support question.
  * **The filtered view is bounded and filtered.** It exists so a human can
    read it; a view containing every tick request is not readable and is the
    reason `_DIAG_NOISY` exists.

And the one that keeps it usable at all: a missing or unreadable log must
still produce a valid reply. The admin is asking BECAUSE something is wrong,
and that is exactly when the log is most likely to be unreadable.
"""
from __future__ import annotations

import pytest

from backend.src.services.cluster.remote import client as rc


@pytest.fixture
def logfile(tmp_path, monkeypatch):
    monkeypatch.setattr("backend.src.config.DATA_DIR", tmp_path)
    return tmp_path / "forex_trader.log"


def _line(level="INFO", msg="something happened", when="2999-01-01 12:00:00"):
    return f"{when},123 {level} some.logger — {msg}"


class TestTheEnvelope:
    def test_it_identifies_the_machine_and_the_build(self, logfile):
        data = rc._build_diagnostics()["data"]

        for key in ("platform", "hostname", "python", "version",
                    "commit_sha", "uptime_s"):
            assert key in data, key

    def test_the_uptime_is_a_number_of_seconds(self, logfile):
        assert isinstance(rc._build_diagnostics()["data"]["uptime_s"], int)


class TestHowMuchLeaves:
    def test_the_raw_tail_is_capped(self, logfile):
        """Unbounded, a long-running install ships its whole log over a
        websocket to answer a support question."""
        logfile.write_text("\n".join(_line(msg=f"line {i}") for i in range(9000)),
                           encoding="utf-8")

        raw = rc._build_diagnostics()["data"]["log_raw"].splitlines()

        assert len(raw) == 3000

    def test_the_raw_tail_is_the_END_of_the_log(self, logfile):
        """The recent lines are the ones that explain a problem. Taking the
        first 3,000 would ship the startup of an install that has been up for
        a month."""
        logfile.write_text("\n".join(_line(msg=f"line {i}") for i in range(9000)),
                           encoding="utf-8")

        raw = rc._build_diagnostics()["data"]["log_raw"]

        assert "line 8999" in raw
        assert "line 0 " not in raw

    def test_the_filtered_view_is_capped(self, logfile):
        logfile.write_text("\n".join(_line("ERROR", f"boom {i}") for i in range(2000)),
                           encoding="utf-8")

        assert len(rc._build_diagnostics()["data"]["log_lines"]) == 500


class TestWhatTheFilteredViewKeeps:
    def test_errors_and_warnings_are_kept(self, logfile):
        logfile.write_text("\n".join([_line("ERROR", "a bad thing"),
                                      _line("WARNING", "a worrying thing")]),
                           encoding="utf-8")

        text = "\n".join(rc._build_diagnostics()["data"]["log_lines"])

        assert "a bad thing" in text and "a worrying thing" in text

    def test_ordinary_info_is_kept(self, logfile):
        logfile.write_text(_line("INFO", "engine started"), encoding="utf-8")

        text = "\n".join(rc._build_diagnostics()["data"]["log_lines"])

        assert "engine started" in text

    @pytest.mark.parametrize("noisy", ["GET /tick/XAUUSD", "GET /account",
                                       "POST /order", "HTTP/1.0 200 OK"])
    def test_per_request_chatter_is_dropped(self, logfile, noisy):
        """A view containing every tick request is not readable, which is the
        whole reason the filter exists."""
        logfile.write_text("\n".join([_line("INFO", noisy),
                                      _line("INFO", "engine started")]),
                           encoding="utf-8")

        text = "\n".join(rc._build_diagnostics()["data"]["log_lines"])

        assert noisy not in text
        assert "engine started" in text

    def test_a_noisy_line_at_ERROR_is_still_kept(self, logfile):
        """The noise filter applies to INFO only. An error mentioning a tick
        request is still an error."""
        logfile.write_text(_line("ERROR", "GET /tick/XAUUSD failed hard"),
                           encoding="utf-8")

        text = "\n".join(rc._build_diagnostics()["data"]["log_lines"])

        assert "failed hard" in text

    def test_the_view_is_in_chronological_order(self, logfile):
        """It is built by walking backwards and reversed at the end. A view
        in reverse order reads as an effect before its cause."""
        logfile.write_text("\n".join([_line("ERROR", "first"),
                                      _line("ERROR", "second")]),
                           encoding="utf-8")

        lines = rc._build_diagnostics()["data"]["log_lines"]

        assert lines.index([l for l in lines if "first" in l][0]) < \
               lines.index([l for l in lines if "second" in l][0])

    def test_anything_older_than_a_day_is_left_out(self, logfile):
        logfile.write_text("\n".join([_line("ERROR", "ancient", "2020-01-01 00:00:00"),
                                      _line("ERROR", "recent")]),
                           encoding="utf-8")

        text = "\n".join(rc._build_diagnostics()["data"]["log_lines"])

        assert "ancient" not in text
        assert "recent" in text


class TestWhenTheLogIsNotThere:
    def test_a_missing_log_still_produces_a_reply(self, logfile):
        """The admin is asking BECAUSE something is wrong. An exception here
        means the request goes unanswered at exactly the wrong moment."""
        data = rc._build_diagnostics()["data"]

        assert data["log_lines"] == [] and data["log_raw"] == ""
        assert data["hostname"]

    def test_an_unreadable_log_still_produces_a_reply(self, logfile,
                                                       monkeypatch):
        logfile.write_text("anything", encoding="utf-8")

        def _boom(*a, **kw):
            raise OSError("permission denied")
        monkeypatch.setattr(type(logfile), "read_text", _boom, raising=False)

        assert rc._build_diagnostics()["data"]["hostname"]


class TestTheLevelParser:
    @pytest.mark.parametrize("level", ["INFO", "ERROR", "WARNING", "DEBUG",
                                       "CRITICAL"])
    def test_every_level_is_read(self, level):
        """The old implementation sliced [24:31] and only worked for WARNING,
        silently dropping INFO and ERROR -- the two that matter most."""
        assert rc._log_level(_line(level)) == level

    def test_a_short_line_is_not_a_level(self):
        assert rc._log_level("too short") == ""

    def test_a_continuation_line_is_not_a_level(self):
        """Tracebacks are indented continuations of the line above."""
        assert rc._log_level("    File \"x.py\", line 1, in <module>") == ""
