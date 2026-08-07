"""The app must own its port at the moment it binds, not seconds earlier.

main() freed the port once at the top, then opened the database and imported
forex_trader.ui.app before ui.run() finally bound -- several seconds later.
Anything that claimed the port inside that window won, and the losing instance
died on a bare uvicorn "[Errno 48] address already in use" with its Terminal
window closing on the error.

A licence activation is precisely when two instances exist: the activation
restart spawns a delayed relaunch, and a user seeing nothing happen launches
the app themselves. Confirmed live 2026-08-07 on a remote Mac -- it activated,
restarted, and never came back, with neither the link nor the launcher able to
bring it up.

These tests use real listening sockets in real subprocesses.
"""
import socket
import subprocess
import sys
import time

import pytest

import run
from forex_trader.core.platform_utils import is_port_listening


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def squatter():
    """A separate process holding a port open, as a rival instance would."""
    procs = []

    def _start(port: int) -> subprocess.Popen:
        p = subprocess.Popen(
            [sys.executable, "-c",
             "import socket,time\n"
             "s=socket.socket(socket.AF_INET, socket.SOCK_STREAM)\n"
             "s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
             f"s.bind(('0.0.0.0', {port}))\n"
             "s.listen(5)\n"
             "time.sleep(120)\n"],
        )
        procs.append(p)
        for _ in range(100):
            if is_port_listening(port):
                return p
            time.sleep(0.05)
        raise AssertionError(f"squatter never started listening on {port}")

    yield _start
    for p in procs:
        p.kill()
        p.wait(timeout=10)


def test_claim_port_takes_a_port_held_by_another_process(squatter):
    port = _free_port()
    proc = squatter(port)
    assert is_port_listening(port), "precondition: the rival holds the port"

    assert run._claim_port(port, timeout=10.0) is True
    assert not is_port_listening(port), "port must be free once claimed"
    assert proc.poll() is not None, "the process holding it must be gone"


def test_claim_port_returns_true_when_nothing_holds_it():
    port = _free_port()
    assert not is_port_listening(port)
    start = time.time()
    assert run._claim_port(port, timeout=10.0) is True
    assert time.time() - start < 2.0, "a free port must not wait out the timeout"


def test_claim_port_gives_up_rather_than_hanging_forever(monkeypatch):
    """If the holder cannot be killed, report failure so main() can print
    something actionable instead of dying inside uvicorn."""
    port = _free_port()
    monkeypatch.setattr(run, "_free_port", lambda _p: None)
    monkeypatch.setattr(
        "forex_trader.core.platform_utils.is_port_listening", lambda _p: True
    )

    start = time.time()
    assert run._claim_port(port, timeout=1.5) is False
    elapsed = time.time() - start
    assert 1.4 < elapsed < 6.0, f"should give up near the timeout, took {elapsed:.1f}s"
