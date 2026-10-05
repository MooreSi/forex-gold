"""The EA counts silence only while it was free to listen (handover 045,
bugs/071 #1).

The app only ever answers the EA's pings. The EA sends them from OnTimer,
and OrderSend blocks its only thread for as long as the broker takes (up to
MT5's 180 s request timeout). After a slow fill the next PollSocket found no
reply for over 10 s -- because it had sent no ping -- and dropped the link,
taking the fill confirmation with it. 2026-09-29: every "EA link lost" that
day followed an order by about 10 s.

PollSocket now restarts the silence clock when it was called late (more than
3 s after the previous call, against a 200 ms timer), so the app gets its
full 10 s to answer the ping that is sent next.

MQL cannot run here; this reads the source, as the other EA tests do.
"""
import re
from pathlib import Path

SRC = (Path(__file__).resolve().parents[2] / "mql5" / "ForexTraderBridge.mq5").read_text()


def _poll_socket_body() -> str:
    start = SRC.index("void PollSocket()")
    return SRC[start:SRC.index("\nvoid ", start + 1)]


def test_a_late_poll_restarts_the_silence_clock():
    body = _poll_socket_body()
    assert re.search(r"g_lastPollAt\s*>\s*0\s*&&\s*now\s*-\s*g_lastPollAt\s*>\s*3000\)\s*g_lastRecv\s*=\s*now", body)


def test_it_happens_before_the_silence_check():
    body = _poll_socket_body()
    assert body.index("g_lastPollAt = now") < body.index("GetTickCount64() - g_lastRecv > 10000")


def test_the_version_moved_with_the_behaviour():
    assert '#define EA_VERSION "1.10"' in SRC
    assert '#property version   "1.10"' in SRC
    assert '#define EA_VERSION_DATE "2026-10-05"' in SRC
