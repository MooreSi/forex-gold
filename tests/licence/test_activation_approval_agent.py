"""Who is allowed to run a Telegram approval poller on the activation screen.

bugs/021. The poller itself lives in `services.telegram.activation_bot`; this
is about the wiring, and the wiring carries a security property that the poller
cannot enforce on its own:

  **only the admin machine starts it.**

A machine that is merely a client has no business polling for approvals -- it
cannot issue a licence, and a poller there would compete for the bot token with
the real admin. So the agent is registered by `run.py` and started by the guard
only on the admin path.

`config/` sits at the bottom of the import stack, so the guard cannot import
`services.telegram` to do this itself; `run.py` injects a callable instead.
That is the same pattern the config-boundary rule prescribes, and it is why
this is a registration hook rather than a direct call.
"""
from __future__ import annotations

import pytest

from backend.src.config.licence import guard


class _App:
    """Captures the handlers the guard registers with @ng_app.on_startup."""

    def __init__(self):
        self.handlers = []

    def on_startup(self, fn):
        self.handlers.append(fn)
        return fn

    def run_all(self):
        for fn in self.handlers:
            fn()


@pytest.fixture
def agents(monkeypatch):
    started: list = []
    monkeypatch.setattr(guard, "_activation_agents", [])
    guard.register_activation_agent(lambda: started.append("ran"))
    # The admin path also starts the remote server; stub it out.
    import backend.src.services.cluster.remote.server as rs
    monkeypatch.setattr(rs, "start", lambda: None)
    return started


class TestTheAdminMachine:
    def test_the_registered_agent_is_started(self, agents):
        app = _App()

        guard._start_agents_for_activation(app, is_admin=True, known_client=False)
        app.run_all()

        assert agents == ["ran"]


class TestEveryOtherMachine:
    def test_a_known_client_does_not_start_it(self, agents):
        """It cannot issue a licence, and polling would fight the real admin
        for the bot token."""
        app = _App()
        import backend.src.services.cluster.remote.client as rc
        original = rc.start
        rc.start = lambda: None
        try:
            guard._start_agents_for_activation(app, is_admin=False,
                                               known_client=True)
            app.run_all()
        finally:
            rc.start = original

        assert agents == []

    def test_an_unknown_machine_does_not_start_it(self, agents):
        app = _App()

        guard._start_agents_for_activation(app, is_admin=False,
                                           known_client=False)
        app.run_all()

        assert agents == []


class TestItCannotStrandTheMachine:
    def test_an_agent_that_raises_does_not_take_the_screen_down(self, monkeypatch):
        """This screen is the only way back in. An exception here replaces it
        with a traceback and strands the install."""
        monkeypatch.setattr(guard, "_activation_agents", [])

        def _boom():
            raise RuntimeError("no network")

        guard.register_activation_agent(_boom)
        import backend.src.services.cluster.remote.server as rs
        monkeypatch.setattr(rs, "start", lambda: None)
        app = _App()

        guard._start_agents_for_activation(app, is_admin=True, known_client=False)
        app.run_all()   # must not raise

    def test_one_failing_agent_does_not_stop_the_next(self, monkeypatch):
        monkeypatch.setattr(guard, "_activation_agents", [])
        ran = []

        def _boom():
            raise RuntimeError("no network")

        guard.register_activation_agent(_boom)
        guard.register_activation_agent(lambda: ran.append("second"))
        import backend.src.services.cluster.remote.server as rs
        monkeypatch.setattr(rs, "start", lambda: None)
        app = _App()

        guard._start_agents_for_activation(app, is_admin=True, known_client=False)
        app.run_all()

        assert ran == ["second"]


class TestRunPyRegistersIt:
    def test_the_approval_loop_is_registered_before_the_licence_check(self):
        """Registration has to happen before `enforce()`, because enforce()
        shows the screen and never returns."""
        import pathlib
        import re

        src = pathlib.Path("run.py").read_text(encoding="utf-8")
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        reg = code.index("register_activation_agent")
        enf = code.index("_licence_enforce()")
        assert reg < enf, "agent must be registered before enforce() blocks"
        assert re.search(r"activation_bot", code)
