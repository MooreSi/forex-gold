"""Only one FOREX Trader may run against one data directory at a time.

Two checkouts live on this machine -- the original app and the React one --
and `backend.src.config` resolves BOTH to the same `USER_DATA_DIR`. Same
`config.yaml`, same `forex_trader_<env>.db`, same `reversal_engine.db`, same
MT5 bridge port. Nothing in the code could tell them apart; the config
module's own comment has said so since 2026-07-21.

What stood in for a lock was `run._free_port()`, which *kills* whatever holds
the port. That is the wrong way round: launching the second app silently
terminated the first, which on a live account is a trading process being shot
mid-flight. The second instance must refuse to start instead.

The lock is an OS advisory lock on a byte of a file in the shared data
directory, not a pid file with staleness rules: the kernel drops it when the
holder dies, so a crash or a SIGKILL cannot leave an install that will not
start. The tests below use real subprocesses because that is the only way the
claim ("another PROCESS cannot take it") means anything.
"""
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import backend.src.config as config
from backend.src.utils import single_instance

REPO = Path(__file__).resolve().parents[2]


@pytest.fixture
def data_dir(tmp_path, monkeypatch):
    """A throwaway stand-in for USER_DATA_DIR.

    Never the real one: the app's data directory holds the live demo database
    and this suite runs unattended. Patched on the config module rather than
    through FOREX_TRADER_DATA_DIR, because config resolves USER_DATA_DIR once
    at import and this process imported it long ago; the env var is still set
    for the subprocesses below, which do get a fresh import.
    """
    d = tmp_path / "ForexTrader"
    d.mkdir()
    monkeypatch.setattr(config, "USER_DATA_DIR", d)
    monkeypatch.setenv("FOREX_TRADER_DATA_DIR", str(d))
    return d


@pytest.fixture
def holder(data_dir):
    """A separate process holding the lock, as a rival instance would."""
    procs = []

    def _start() -> subprocess.Popen:
        p = subprocess.Popen(
            [sys.executable, "-c",
             "import sys, time\n"
             f"sys.path.insert(0, {str(REPO)!r})\n"
             "from backend.src.utils import single_instance\n"
             "single_instance.acquire()\n"
             "print('held', flush=True)\n"
             "time.sleep(60)\n"],
            stdout=subprocess.PIPE, text=True,
            env={**os.environ, "FOREX_TRADER_DATA_DIR": str(data_dir)},
        )
        assert p.stdout.readline().strip() == "held", "holder never acquired"
        procs.append(p)
        return p

    yield _start
    for p in procs:
        p.kill()
        p.wait(timeout=10)


class TestTheLockIsSharedBetweenCheckouts:
    def test_it_lives_in_the_data_dir_not_the_repo(self, data_dir):
        """The two checkouts share USER_DATA_DIR and nothing else.

        A lock file beside the code would be per-checkout, so each app would
        take its own and both would still run -- the exact thing being
        prevented.
        """
        path = single_instance.lock_path()
        assert path.parent == data_dir
        assert REPO not in path.parents

    def test_it_is_resolved_per_call_not_frozen_at_import(self, tmp_path, monkeypatch):
        """FOREX_TRADER_DATA_DIR is the documented way to run two installs
        side by side deliberately, and it lands on config.USER_DATA_DIR. A
        lock path captured at import would ignore it and close that escape
        hatch."""
        other = tmp_path / "Other"
        other.mkdir()
        monkeypatch.setattr(config, "USER_DATA_DIR", other)
        assert single_instance.lock_path().parent == other


class TestASecondInstanceIsRefused:
    def test_another_process_cannot_take_a_held_lock(self, holder):
        holder()
        with pytest.raises(single_instance.AlreadyRunning):
            single_instance.acquire()

    def test_the_refusal_names_the_holder(self, holder):
        """The message a user sees has to be actionable. 'Already running'
        without a pid or a checkout path, on a machine with two checkouts, does
        not tell them which app to close."""
        p = holder()
        with pytest.raises(single_instance.AlreadyRunning) as exc:
            single_instance.acquire()
        assert exc.value.pid == p.pid
        assert str(exc.value.root)
        assert str(p.pid) in str(exc.value)

    def test_the_holder_is_readable_while_it_is_held(self, holder):
        p = holder()
        info = single_instance.holder()
        assert info is not None and info["pid"] == p.pid

    def test_acquiring_twice_in_one_process_is_not_a_refusal(self, data_dir):
        """`main()` is re-entered by the POSIX restart path, which is an
        os.execv -- same pid, new image. A process must never deadlock against
        itself."""
        single_instance.acquire()
        try:
            single_instance.acquire()
        finally:
            single_instance.release()


class TestTheLockOutlivesNothing:
    def test_killing_the_holder_frees_it(self, holder):
        """No staleness heuristic: a -9'd or crashed app must not leave an
        install that refuses to start. The kernel drops the lock with the
        process."""
        p = holder()
        with pytest.raises(single_instance.AlreadyRunning):
            single_instance.acquire()
        p.kill()
        p.wait(timeout=10)
        single_instance.acquire(timeout=10)
        single_instance.release()

    def test_release_lets_the_next_instance_in(self, data_dir):
        single_instance.acquire()
        single_instance.release()
        single_instance.acquire()
        single_instance.release()

    def test_it_waits_for_a_handover_rather_than_refusing_instantly(self, holder):
        """A restart is two instances overlapping on purpose: the Windows
        path spawns the replacement and only then exits. With no wait, the
        replacement loses the race with its own parent and the app is simply
        gone -- the 2026-08-07 failure that `_claim_port` was written for,
        reproduced one layer up."""
        p = holder()
        started = time.time()
        with pytest.raises(single_instance.AlreadyRunning):
            single_instance.acquire(timeout=1.5)
        assert time.time() - started >= 1.4


class TestTheLockFileItself:
    def test_it_records_who_holds_it(self, data_dir):
        single_instance.acquire()
        try:
            written = json.loads(single_instance.lock_path().read_text(encoding="utf-8"))
        finally:
            single_instance.release()
        assert written["pid"] == os.getpid()
        assert Path(written["root"]) == REPO
        assert written["version"]

    def test_a_leftover_file_is_not_itself_a_lock(self, data_dir):
        """The file persists after a clean exit -- deliberately, so `holder()`
        can still be read. It must not be mistaken for a held lock."""
        single_instance.acquire()
        single_instance.release()
        assert single_instance.lock_path().exists()
        assert single_instance.holder() is None
        single_instance.acquire()
        single_instance.release()
