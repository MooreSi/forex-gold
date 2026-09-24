"""How long the broker takes to execute, per server, from MetaTrader's own logs.

The 2026-09-24 latency audit found the demo server executing with a median
of 0.12 s but a 90th percentile of 8.2 s and a worst case of 43 s -- slow
enough that the EA's 15 s acknowledgement window lapsed on orders that were
in fact placed. Whether the LIVE server behaves the same decides what the fix
is, and the terminal logs already hold the answer: every execution ends in
"done in N ms", and every session begins "authorized on <server>".

The tool reads those logs and nothing else. It places, modifies and closes
nothing, and reaches no broker.
"""
from __future__ import annotations

from tools import order_latency_report as olr

LOG = [
    "KR\t0\t08:00:00.000\tNetwork\t'111': authorized on Broker-Demo through AS04 (ping: 89.87 ms, build 5830)",
    "CL\t0\t08:01:00.000\tTrades\t'111': order #1 buy 0.1 / 0.1 XAUUSD at 1.0 done in 100.000 ms",
    "CL\t0\t08:02:00.000\tTrades\t'111': modify #1 buy 0.1 XAUUSD sl: 1.0, tp: 2.0 -> sl: 1.1, tp: 2.0 done in 9000.000 ms",
    "KR\t0\t09:00:00.000\tNetwork\t'222': authorized on Broker-Live 6 through AS01 (ping: 20.00 ms, build 5830)",
    "CL\t0\t09:01:00.000\tTrades\t'222': order #2 sell 0.1 / 0.1 XAUUSD at 1.0 done in 50.500 ms",
    "CL\t0\t09:02:00.000\tTrades\t'222': order #3 sell 0.1 / 0.1 XAUUSD at 1.0 done in 70.000 ms",
    "XX\t0\t09:03:00.000\tTrades\t'222': market buy 0.1 XAUUSD sl: 1.0 tp: 2.0",
]


def test_each_execution_is_credited_to_the_server_it_ran_on():
    rows = olr.parse_lines(LOG)

    assert [(r.server, r.kind, r.ms) for r in rows] == [
        ("Broker-Demo", "order", 100.0),
        ("Broker-Demo", "modify", 9000.0),
        ("Broker-Live 6", "order", 50.5),
        ("Broker-Live 6", "order", 70.0),
    ]


def test_a_line_that_is_not_a_finished_execution_is_ignored():
    assert olr.parse_lines([LOG[-1]]) == []


def test_an_execution_before_any_login_is_filed_as_unknown():
    rows = olr.parse_lines([LOG[1]])
    assert rows[0].server == "unknown"


def test_the_summary_gives_count_median_p90_max_and_the_slow_share():
    summary = olr.summarise(olr.parse_lines(LOG))

    live = summary["Broker-Live 6"]
    assert live["n"] == 2 and live["max_ms"] == 70.0
    demo = summary["Broker-Demo"]
    assert demo["n"] == 2
    assert demo["over_5s"] == 1
    assert demo["max_ms"] == 9000.0


def test_it_reads_the_utf16_files_metatrader_writes(tmp_path):
    f = tmp_path / "20260923.log"
    f.write_text("\n".join(LOG) + "\n", encoding="utf-16")

    assert len(olr.parse_files([f])) == 4


def test_the_server_carries_over_into_the_next_day_s_file(tmp_path):
    """A session outlives midnight: the login is in yesterday's file and the
    executions are in today's. Resetting per file filed most of the history as
    "unknown" on the first real run."""
    (tmp_path / "20260922.log").write_text(LOG[0] + "\n", encoding="utf-16")
    (tmp_path / "20260923.log").write_text(LOG[1] + "\n", encoding="utf-16")

    rows = olr.parse_files([tmp_path / "20260923.log", tmp_path / "20260922.log"])

    assert [r.server for r in rows] == ["Broker-Demo"]
