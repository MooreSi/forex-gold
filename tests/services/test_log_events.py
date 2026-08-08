"""The live-diagnostics log filter, pinned.

This was ~90 lines nested inside `render()` in `frontend/pages/settings.py`,
reachable only by rendering a NiceGUI page. Moving it to
`services/health/log_events.py` is what makes these assertions possible.

The behaviour that matters: only lines from the *current* run are shown, and
the noise filter wins over the keep filter. Both are easy to invert without
any test noticing, and either way the panel silently shows the wrong thing.
"""
from __future__ import annotations

import pytest

from backend.src.services.health import log_events


@pytest.fixture
def log_file(tmp_path, monkeypatch):
    """Point the service at a temp log file via the config module it imports."""
    import backend.src.config as cfg
    monkeypatch.setattr(cfg, "DATA_DIR", str(tmp_path), raising=False)
    path = tmp_path / "forex_trader.log"

    def _write(lines):
        path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return path
    return _write


def test_lines_from_a_previous_run_are_excluded(log_file):
    """Only events since the last "SimulationEngine started" belong to now."""
    log_file([
        "2026-08-01 09:00:00 ERROR  old run, must not appear",
        "2026-08-02 10:00:00 INFO   SimulationEngine started",
        "2026-08-02 10:00:05 ERROR  current run, must appear",
    ])
    got = [line for _, line in log_events.since_last_start()]
    assert any("current run" in ln for ln in got)
    assert not any("old run" in ln for ln in got), (
        "a line older than the last startup marker leaked into the panel"
    )


def test_the_drop_filter_beats_the_keep_filter(log_file):
    """A DEBUG line mentioning a kept keyword must still be suppressed --
    otherwise routine polling floods the panel it exists to keep readable."""
    log_file([
        "2026-08-02 10:00:00 INFO   SimulationEngine started",
        "2026-08-02 10:00:01 DEBUG  Trade opened chatter",
        "2026-08-02 10:00:02 INFO   HTTP Request: GET /tick positions",
    ])
    got = [line for _, line in log_events.since_last_start()]
    # The startup marker itself matches the keep filter and is meant to show.
    assert got == ["2026-08-02 10:00:00 INFO   SimulationEngine started"]
    assert not any("chatter" in ln for ln in got), "DEBUG line survived the drop filter"
    assert not any("HTTP Request" in ln for ln in got), "poll noise survived the drop filter"


def test_errors_and_warnings_are_classified_by_level(log_file):
    log_file([
        "2026-08-02 10:00:00 INFO   SimulationEngine started",
        "2026-08-02 10:00:01 ERROR  bridge offline",
        "2026-08-02 10:00:02 WARNING slow tick",
        "2026-08-02 10:00:03 CRITICAL meltdown",
        "2026-08-02 10:00:04 INFO   NEW SIGNAL generated",
    ])
    levels = [lvl for lvl, _ in log_events.since_last_start()]
    assert levels == ["event", "error", "warning", "error", "event"]


def test_a_continuation_line_with_no_timestamp_is_kept(log_file):
    """Stack-trace bodies carry no timestamp of their own; dropping them
    would show the exception header with none of the traceback."""
    log_file([
        "2026-08-02 10:00:00 INFO   SimulationEngine started",
        "2026-08-02 10:00:01 ERROR  boom",
        "    File \"x.py\", line 1, in <module>  ERROR context",
    ])
    got = [line for _, line in log_events.since_last_start()]
    assert any("File " in ln for ln in got)


def test_a_missing_log_file_is_not_an_error(log_file, tmp_path, monkeypatch):
    import backend.src.config as cfg
    monkeypatch.setattr(cfg, "DATA_DIR", str(tmp_path / "nothing-here"), raising=False)
    assert log_events.since_last_start() == []


def test_output_is_capped_to_the_last_n_lines(log_file):
    """The panel refreshes every 5s; an unbounded list would grow without limit."""
    lines = ["2026-08-02 10:00:00 INFO   SimulationEngine started"]
    lines += [f"2026-08-02 10:00:01 ERROR  event {i}" for i in range(log_events.MAX_LINES + 50)]
    log_file(lines)
    got = log_events.since_last_start()
    assert len(got) == log_events.MAX_LINES
    assert "event " + str(log_events.MAX_LINES + 49) in got[-1][1], "the cap must keep the NEWEST lines"
