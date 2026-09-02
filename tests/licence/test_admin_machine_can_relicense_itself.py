"""The machine that issues licences must be able to issue one to itself.

Hit for real on 2026-09-02. The owner's Mac lost its licence file, restarted,
and could not get back in:

    10:09:52  No valid licence found — showing activation screen.
    10:09:53  [RemoteClient] Agent started (connecting to 217.155.25.160:8443)
    10:10:01  Connection error: ConnectionRefusedError: [Errno 61]

A deadlock, and a complete one:

  1. no licence, so `guard.enforce()` shows the activation screen and startup
     stops there -- `run.py` calls it BEFORE `backend.src.app.startup()`;
  2. the screen starts the remote CLIENT so an admin can push a licence down;
  3. the client dials the admin server;
  4. **the admin server is this same machine**, and it is started from
     `startup()`, which step 1 never reached;
  5. connection refused, for ever.

`ADMIN_AVAILABLE` and `password_is_set()` were both true the whole time. The
issuer simply never ran, because the issuer will not start until it is
licensed.

So the activation screen starts the admin server too, when this machine is
the admin machine. The console can then issue a licence to itself and the
existing self-heal push does the rest.

The guard's own rule is untouched: this does not grant, weaken or bypass a
licence. It starts the thing that can legitimately issue one.
"""
from __future__ import annotations

import pytest

from backend.src.config.licence import guard


class _App:
    """Stands in for nicegui's app: records the on_startup hooks registered."""

    def __init__(self):
        self.hooks: list = []

    def on_startup(self, fn):
        self.hooks.append(fn)
        return fn

    def get(self, _path):
        return lambda fn: fn


@pytest.fixture
def started(monkeypatch):
    """Capture which of the two agents the activation screen starts."""
    calls: list = []
    monkeypatch.setattr(
        "backend.src.services.cluster.remote.client.start",
        lambda: calls.append("client"))
    monkeypatch.setattr(
        "backend.src.services.cluster.remote.server.start",
        lambda: calls.append("server"))
    return calls


def _run_hooks(app):
    for fn in app.hooks:
        fn()


class TestOnTheAdminMachine:
    def test_the_admin_server_is_started(self, started, monkeypatch):
        """Without it there is nothing for the activation screen to talk to,
        and the machine can never be relicensed."""
        app = _App()

        guard._start_agents_for_activation(app, is_admin=True, known_client=False)
        _run_hooks(app)

        assert "server" in started

    def test_the_client_is_not_started_as_well(self, started, monkeypatch):
        """The admin Mac does not connect to itself. Starting both would have
        it dial its own port and log a refusal on every retry."""
        app = _App()

        guard._start_agents_for_activation(app, is_admin=True, known_client=True)
        _run_hooks(app)

        assert "client" not in started


class TestOnAnOrdinaryMachine:
    def test_the_client_is_started_when_it_has_a_token(self, started):
        """Unchanged behaviour: a known client connects so an admin-pushed
        licence can self-heal the install."""
        app = _App()

        guard._start_agents_for_activation(app, is_admin=False, known_client=True)
        _run_hooks(app)

        assert started == ["client"]

    def test_nothing_is_started_without_a_token(self, started):
        """A machine that has never registered has nothing to connect with;
        it registers from the screen instead."""
        app = _App()

        guard._start_agents_for_activation(app, is_admin=False, known_client=False)
        _run_hooks(app)

        assert started == []

    def test_the_admin_server_is_never_started_here(self, started):
        """It refuses to bind on a non-admin machine anyway, but it must not
        be asked to: this is the server that issues licence keys."""
        app = _App()

        guard._start_agents_for_activation(app, is_admin=False, known_client=True)
        _run_hooks(app)

        assert "server" not in started


class TestItCannotBreakTheScreen:
    @pytest.mark.parametrize("is_admin", [True, False])
    def test_a_failure_to_start_leaves_the_screen_up(self, monkeypatch, is_admin):
        """The activation screen is the only way back in. An exception here
        would replace it with a traceback and strand the machine."""
        def _boom():
            raise RuntimeError("no")
        monkeypatch.setattr(
            "backend.src.services.cluster.remote.client.start", _boom)
        monkeypatch.setattr(
            "backend.src.services.cluster.remote.server.start", _boom)
        app = _App()

        guard._start_agents_for_activation(app, is_admin=is_admin, known_client=True)
        _run_hooks(app)


class TestTheDeadlockItself:
    def test_the_guard_runs_before_startup(self):
        """States the shape of the bug. `run.py` enforces the licence before
        `backend.src.app.startup()`, so anything startup() begins is
        unreachable from the activation screen -- which is why the screen has
        to start the server itself rather than relying on startup."""
        import pathlib

        src = pathlib.Path("run.py").read_text(encoding="utf-8")

        # The CALL site, not the def -- `_start_mt5_bridge` is defined near the
        # top of the file, so matching the bare name finds the definition and
        # the assertion inverts.
        assert src.index("_licence_enforce()") < src.index("bridge_proc = _start_mt5_bridge()")


class TestDetectingTheAdminMachine:
    """Answered from the filesystem, not by importing upward.

    The obvious implementation asks `backend.src.app.ADMIN_AVAILABLE` and
    `services.cluster.remote.auth.password_is_set`. Both are above `config/`
    in the stack, and the import contract holds `config/` at the bottom -- the
    module that GATES startup must not depend on the module that performs it.
    Adding those two imports regressed the contract from 3 to 5, and the gate
    caught it.
    """

    def test_it_needs_both_keygen_and_a_password(self, tmp_path, monkeypatch):
        monkeypatch.setattr("backend.src.config.USER_DATA_DIR", tmp_path)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "nowhere")

        assert guard._this_is_the_admin_machine() is False

    def test_keygen_without_a_password_is_not_the_admin_machine(
        self, tmp_path, monkeypatch,
    ):
        """The server will not start without a password anyway -- app.py
        requires both. Reporting True here would start an agent that
        immediately declines."""
        home = tmp_path / "home"
        (home / "Documents" / "KeyGen").mkdir(parents=True)
        (home / "Documents" / "KeyGen" / "forex_admin.py").write_text("")
        monkeypatch.setattr("backend.src.config.USER_DATA_DIR", tmp_path / "data")
        monkeypatch.setattr("pathlib.Path.home", lambda: home)

        assert guard._this_is_the_admin_machine() is False

    def test_a_password_without_keygen_is_not_the_admin_machine(
        self, tmp_path, monkeypatch,
    ):
        """The case that separates the two checks. A password hash can outlive
        a KeyGen directory that was moved or removed -- and without KeyGen
        there is no console to issue anything, so starting the server would
        bring up an issuer that cannot issue.

        Mutation found this gap: with only "keygen but no password" and
        "neither" covered, deleting the KeyGen check changed no result.
        """
        data = tmp_path / "data"
        (data / "remote").mkdir(parents=True)
        (data / "remote" / "admin_password.hash").write_text("a-hash")
        monkeypatch.setattr("backend.src.config.USER_DATA_DIR", data)
        monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path / "no-keygen-here")

        assert guard._this_is_the_admin_machine() is False

    def test_an_empty_password_file_does_not_count(self, tmp_path, monkeypatch):
        """`password_is_set` requires a non-empty file, and this must agree
        with it or the two disagree about what an admin machine is."""
        home = tmp_path / "home"
        (home / "Documents" / "KeyGen").mkdir(parents=True)
        (home / "Documents" / "KeyGen" / "forex_admin.py").write_text("")
        data = tmp_path / "data"
        (data / "remote").mkdir(parents=True)
        (data / "remote" / "admin_password.hash").write_text("")
        monkeypatch.setattr("backend.src.config.USER_DATA_DIR", data)
        monkeypatch.setattr("pathlib.Path.home", lambda: home)

        assert guard._this_is_the_admin_machine() is False

    def test_both_present_is_the_admin_machine(self, tmp_path, monkeypatch):
        home = tmp_path / "home"
        (home / "Documents" / "KeyGen").mkdir(parents=True)
        (home / "Documents" / "KeyGen" / "forex_admin.py").write_text("")
        data = tmp_path / "data"
        (data / "remote").mkdir(parents=True)
        (data / "remote" / "admin_password.hash").write_text("a-hash")
        monkeypatch.setattr("backend.src.config.USER_DATA_DIR", data)
        monkeypatch.setattr("pathlib.Path.home", lambda: home)

        assert guard._this_is_the_admin_machine() is True

    def test_it_agrees_with_the_two_sources_it_replaces(self):
        """The point of the whole function: it must give the same answer as
        app.ADMIN_AVAILABLE and auth.password_is_set on this machine, or it is
        a second definition of "admin machine" that will drift."""
        from backend.src.app import ADMIN_AVAILABLE
        from backend.src.services.cluster.remote.auth import password_is_set

        assert guard._this_is_the_admin_machine() == bool(
            ADMIN_AVAILABLE and password_is_set())

    def test_it_never_raises(self, monkeypatch):
        """It decides which agent the activation screen brings up, and that
        screen is the only way back in."""
        def _boom(*a, **kw):
            raise OSError("filesystem gone")
        monkeypatch.setattr("pathlib.Path.exists", _boom)

        assert guard._this_is_the_admin_machine() is False
