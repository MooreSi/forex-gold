"""The support log bundle: the logs, minus the noise, with the context.

The NiceGUI app had an Export Logs button that read the rotated log files,
filtered them, and emailed the result. The React port had nothing, so the
only way to get logs off a machine was to find them on disk.

**This builds the bundle; it does not send it anywhere.** The original mailed
it to a hardcoded address. A dashboard in a browser can simply hand the
operator the file, which needs no email provider configured and puts nobody's
address in the code -- so this is a download, and that difference is
deliberate.

What makes the bundle useful is what it leaves out. A raw log is mostly
`HTTP Request: GET /tick/XAUUSD` at several lines a second; the errors are in
there somewhere. Dropping the polling and the DEBUG lines is what turns a
25 MB file into something readable, and the header says how much was dropped
so nobody wonders whether the quiet is real.
"""
from __future__ import annotations

import time

import pytest

from backend.src.services.diagnostics import log_bundle


_NOW = 1_789_900_000.0   # a fixed "now" so the day window is deterministic


def _stamp(offset_days: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S",
                         time.localtime(_NOW - offset_days * 86400)) + ",123"


def _words(i: int) -> str:
    """A distinct phrase per line.

    The size-cap fixtures used `line {i}`, which the burst collapse now
    compresses to a single line plus a note -- so the bundle never reached the
    cap and the tests stopped exercising it. Distinct words keep them honest.
    """
    alphabet = "alpha bravo charlie delta echo foxtrot golf hotel".split()
    return "-".join(alphabet[(i // 8 ** n) % 8] for n in range(4))


def _write(tmp_path, name, lines):
    (tmp_path / name).write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def logs(tmp_path):
    _write(tmp_path, "forex_trader.log", [
        f"{_stamp(0.1)} INFO backend.src.app — SimulationEngine started",
        f"{_stamp(0.1)} DEBUG backend.src.thing — a debug line",
        f"{_stamp(0.1)} INFO httpx — HTTP Request: GET http://localhost:9000/tick/XAUUSD 200",
        f"{_stamp(0.1)} ERROR backend.src.api.errors — something broke",
    ])
    return tmp_path


# ── What it keeps ────────────────────────────────────────────────────────────

def test_an_error_survives(logs):
    out = log_bundle.build(logs, now=_NOW)

    assert "something broke" in out["text"]


def test_app_events_survive(logs):
    out = log_bundle.build(logs, now=_NOW)

    assert "SimulationEngine started" in out["text"]


def test_debug_lines_are_dropped(logs):
    out = log_bundle.build(logs, now=_NOW)

    assert "a debug line" not in out["text"]


def test_polling_noise_is_dropped(logs):
    """Several lines a second, every second, for as long as the app has run.
    They are the reason the raw file is unreadable."""
    out = log_bundle.build(logs, now=_NOW)

    assert "/tick/XAUUSD" not in out["text"]


def test_a_real_http_error_is_not_mistaken_for_polling(logs, tmp_path):
    """Only the GET heartbeats to known endpoints go. A POST, or a failure,
    is exactly what somebody is looking for."""
    _write(tmp_path, "forex_trader.log", [
        f"{_stamp(0.1)} INFO httpx — HTTP Request: POST http://localhost:9000/order 500",
    ])

    out = log_bundle.build(tmp_path, now=_NOW)

    assert "/order" in out["text"]


def test_a_traceback_continuation_is_kept(logs, tmp_path):
    """Continuation lines carry no timestamp of their own. Dropping anything
    unparseable throws away the stack trace and keeps the word "Traceback"."""
    _write(tmp_path, "forex_trader.log", [
        f"{_stamp(0.1)} ERROR backend — boom",
        "Traceback (most recent call last):",
        '  File "x.py", line 1, in <module>',
    ])

    out = log_bundle.build(tmp_path, now=_NOW)

    assert "Traceback (most recent call last):" in out["text"]
    assert 'File "x.py"' in out["text"]


# ── The window ───────────────────────────────────────────────────────────────

def test_lines_older_than_the_window_are_dropped(tmp_path):
    _write(tmp_path, "forex_trader.log", [
        f"{_stamp(30)} ERROR backend — ancient history",
        f"{_stamp(0.1)} ERROR backend — todays problem",
    ])

    out = log_bundle.build(tmp_path, days=5, now=_NOW)

    assert "ancient history" not in out["text"]
    assert "todays problem" in out["text"]


def test_rotated_files_are_read_oldest_first(tmp_path):
    """TimedRotatingFileHandler names backups forex_trader.log.YYYY-MM-DD.
    Read in the wrong order the bundle tells the story backwards."""
    _write(tmp_path, "forex_trader.log.2026-09-18", [f"{_stamp(2)} ERROR backend — first"])
    _write(tmp_path, "forex_trader.log.2026-09-19", [f"{_stamp(1)} ERROR backend — second"])
    _write(tmp_path, "forex_trader.log", [f"{_stamp(0.1)} ERROR backend — third"])

    text = log_bundle.build(tmp_path, now=_NOW)["text"]

    assert text.index("first") < text.index("second") < text.index("third")


def test_a_missing_log_file_is_reported_not_raised(tmp_path):
    out = log_bundle.build(tmp_path, now=_NOW)

    assert out["kept_lines"] == 0
    assert "no log file" in out["note"].lower()


# ── Repeats ──────────────────────────────────────────────────────────────────

def test_consecutive_repeats_are_collapsed(tmp_path):
    """`POST /modify` fires every 5 seconds for as long as a trade is open."""
    _write(tmp_path, "forex_trader.log", [
        f"{_stamp(0.1)} INFO backend — modify sent"
        for _ in range(50)
    ])

    out = log_bundle.build(tmp_path, now=_NOW)

    assert out["text"].count("modify sent") == 1
    assert "repeated 49 more time(s)" in out["text"]


def test_a_repeat_that_is_not_consecutive_is_kept(tmp_path):
    _write(tmp_path, "forex_trader.log", [
        f"{_stamp(0.1)} INFO backend — modify sent",
        f"{_stamp(0.1)} ERROR backend — something else",
        f"{_stamp(0.1)} INFO backend — modify sent",
    ])

    out = log_bundle.build(tmp_path, now=_NOW)

    assert out["text"].count("modify sent") == 2


# ── The header ───────────────────────────────────────────────────────────────

class TestTheHeader:

    def test_it_says_how_much_was_dropped(self, logs):
        """Otherwise a quiet bundle reads as a quiet machine."""
        out = log_bundle.build(logs, now=_NOW)

        assert "Raw lines" in out["text"]
        assert "Kept lines" in out["text"]
        assert "Dropped" in out["text"]

    def test_it_names_the_window(self, logs):
        assert "last 5 days" in log_bundle.build(logs, days=5, now=_NOW)["text"]

    def test_it_carries_the_version_and_platform(self, logs):
        text = log_bundle.build(logs, now=_NOW)["text"]

        assert "App version" in text
        assert "Python" in text
        assert "OS" in text

    def test_a_failure_to_read_the_context_does_not_lose_the_logs(
            self, logs, monkeypatch):
        """The logs are the point. Context is a nicety."""
        monkeypatch.setattr(log_bundle, "_app_context",
                            lambda: (_ for _ in ()).throw(RuntimeError("no db")))

        out = log_bundle.build(logs, now=_NOW)

        assert "something broke" in out["text"]


# ── The size cap ─────────────────────────────────────────────────────────────

class TestTheSizeCap:

    def test_a_big_bundle_is_truncated(self, tmp_path):
        _write(tmp_path, "forex_trader.log", [
            f"{_stamp(0.1)} ERROR backend — failure {_words(i)} " + "x" * 200
            for i in range(2000)
        ])

        out = log_bundle.build(tmp_path, now=_NOW, max_bytes=20_000)

        assert out["truncated"] is True
        assert len(out["text"].encode()) <= 20_000

    def test_it_keeps_the_NEWEST_entries(self, tmp_path):
        """The ones that explain what just went wrong."""
        _write(tmp_path, "forex_trader.log", [
            f"{_stamp(0.1)} ERROR backend — failure {_words(i)} " + "x" * 200
            for i in range(2000)
        ])

        out = log_bundle.build(tmp_path, now=_NOW, max_bytes=20_000)

        assert _words(1999) in out["text"]
        assert _words(0) not in out["text"]

    def test_it_says_that_it_truncated(self, tmp_path):
        _write(tmp_path, "forex_trader.log", [
            f"{_stamp(0.1)} ERROR backend — failure {_words(i)} " + "x" * 200
            for i in range(2000)
        ])

        out = log_bundle.build(tmp_path, now=_NOW, max_bytes=20_000)

        assert "TRUNCATED" in out["text"]

    def test_a_small_bundle_is_not_truncated(self, logs):
        out = log_bundle.build(logs, now=_NOW)

        assert out["truncated"] is False


# ── The filename ─────────────────────────────────────────────────────────────

def test_the_filename_is_dated_so_two_bundles_do_not_collide(logs):
    name = log_bundle.build(logs, now=_NOW)["filename"]

    assert name.startswith("forex_trader_logs_")
    assert name.endswith(".txt")


def test_it_reads_the_apps_own_log_directory_by_default(monkeypatch, tmp_path):
    """The directory is a parameter so a test can redirect it, and it defaults
    here rather than in the controller -- a controller that resolves a path is
    doing work instead of forwarding."""
    _write(tmp_path, "forex_trader.log", [f"{_stamp(0.1)} ERROR backend — from the data dir"])
    monkeypatch.setattr("backend.src.config.DATA_DIR", str(tmp_path))

    out = log_bundle.build(now=_NOW)

    assert "from the data dir" in out["text"]


# ── Noise the first version still let through ────────────────────────────────
# Measured against a real 5-day export on 2026-09-20: 10.9M lines scanned,
# 1.03M kept -- still far too many to read. Two causes, both fixed below.

class TestThePollFilterIsPortAgnostic:
    """The patterns were copied from the NiceGUI app, which hardcoded
    `localhost:9000`. This machine's bridge is on 9010, so ~73,000 heartbeat
    lines went into every export. The port is configurable and differs
    between the live and demo installs, so matching it was never right."""

    @pytest.mark.parametrize("path", ["/positions", "/account", "/health"])
    def test_a_heartbeat_on_any_port_is_dropped(self, tmp_path, path):
        _write(tmp_path, "forex_trader.log", [
            f'{_stamp(0.1)} INFO httpx — HTTP Request: GET '
            f'http://localhost:9010{path} "HTTP/1.0 200 OK"',
        ])

        out = log_bundle.build(tmp_path, now=_NOW)

        assert path not in out["text"]

    def test_a_non_heartbeat_path_on_the_same_port_is_kept(self, tmp_path):
        _write(tmp_path, "forex_trader.log", [
            f'{_stamp(0.1)} INFO httpx — HTTP Request: GET '
            f'http://localhost:9010/order_status "HTTP/1.0 200 OK"',
        ])

        out = log_bundle.build(tmp_path, now=_NOW)

        assert "/order_status" in out["text"]


class TestABurstOfNearlyIdenticalLines:
    """124,569 lines of "Skipping high-risk signal tg_id=<n>" -- 12% of the
    whole export -- survived, because each differs by its id and the collapse
    only matched byte-identical lines.

    Collapsed only above a threshold, and only when consecutive, so a short
    run of genuinely different events keeps its numbers. A flood of tens of
    thousands is not something anybody reads the ids of."""

    def test_a_long_burst_collapses(self, tmp_path):
        _write(tmp_path, "forex_trader.log", [
            f"{_stamp(0.1)} INFO backend — Skipping high-risk signal tg_id={i}"
            for i in range(500)
        ])

        out = log_bundle.build(tmp_path, now=_NOW)

        assert out["text"].count("Skipping high-risk signal") == 1
        assert "499 more similar" in out["text"]

    def test_a_short_run_keeps_every_number(self, tmp_path):
        """Three trades opening in a row is a story, not a flood."""
        _write(tmp_path, "forex_trader.log", [
            f"{_stamp(0.1)} INFO backend — Trade {i} opened at 43{i}0"
            for i in range(3)
        ])

        out = log_bundle.build(tmp_path, now=_NOW)

        for i in range(3):
            assert f"Trade {i} opened" in out["text"]

    def test_different_messages_are_never_merged(self, tmp_path):
        _write(tmp_path, "forex_trader.log", [
            *[f"{_stamp(0.1)} INFO backend — Skipping signal {i}" for i in range(400)],
            f"{_stamp(0.1)} ERROR backend — the thing that actually broke",
        ])

        out = log_bundle.build(tmp_path, now=_NOW)

        assert "the thing that actually broke" in out["text"]
