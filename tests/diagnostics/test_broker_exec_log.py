"""The broker's own execution times, found on this machine for Settings > Latency.

The parser itself is pinned by test_order_latency_report.py, which must keep
passing unmodified now that the tool imports it from here. What these pin is
the part the dashboard adds: finding a terminal's logs without being told
where they are, and saying so when there are none.
"""
from __future__ import annotations

from pathlib import Path

from backend.src.services.diagnostics import broker_exec_log as bel

LOGIN = "KR\t0\t08:00:00.000\tNetwork\t'1': authorized on Broker-Demo through AS04 (ping: 9 ms, build 5830)"
FILL = "CL\t0\t08:01:00.000\tTrades\t'1': order #1 buy 0.1 / 0.1 XAUUSD at 1.0 done in {ms} ms"


def _terminal(home: Path, name: str = "ABC") -> Path:
    data = home / "AppData/Roaming/MetaQuotes/Terminal" / name
    (data / "MQL5/Experts").mkdir(parents=True)
    (data / "logs").mkdir()
    return data / "logs"


def test_it_finds_a_terminals_logs_from_its_data_folder(tmp_path):
    logs = _terminal(tmp_path)
    (logs / "20260925.log").write_text(LOGIN + "\n" + FILL.format(ms="120.0") + "\n",
                                       encoding="utf-16")

    out = bel.summary(home=tmp_path)

    assert out["available"] is True
    assert out["servers"]["Broker-Demo"]["n"] == 1
    assert out["servers"]["Broker-Demo"]["median_ms"] == 120.0


def test_only_the_newest_days_are_read(tmp_path):
    logs = _terminal(tmp_path)
    (logs / "20260901.log").write_text(LOGIN + "\n" + FILL.format(ms="9000.0") + "\n",
                                       encoding="utf-16")
    (logs / "20260925.log").write_text(LOGIN + "\n" + FILL.format(ms="100.0") + "\n",
                                       encoding="utf-16")

    out = bel.summary(days=1, home=tmp_path)

    assert out["servers"]["Broker-Demo"]["max_ms"] == 100.0


def test_no_terminal_is_reported_not_raised(tmp_path):
    out = bel.summary(home=tmp_path)

    assert out["available"] is False
    assert out["servers"] == {}


def test_a_terminal_with_an_empty_logs_folder_is_not_available(tmp_path):
    _terminal(tmp_path)

    assert bel.summary(home=tmp_path)["available"] is False


def test_the_tool_uses_the_same_parser():
    """One parser, two callers. A copy is how the two drift."""
    from tools import order_latency_report as olr

    assert olr.parse_lines is bel.parse_lines
    assert olr.summarise is bel.summarise
