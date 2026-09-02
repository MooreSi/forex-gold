"""What the client does with a version announcement and an update order.

Two messages from the admin server, both untested.

`MSG_VERSION_INFO` sets the "update available" flag the dashboard renders.
Getting it wrong is quiet in both directions: a false positive nags the user
to update to the version they already run, and a false negative means a
machine sits on an old build believing it is current -- which is how the stale
EA build went unnoticed for a week in August.

`MSG_GIT_UPDATE` is the admin pressing "update everyone". The client must
acknowledge BEFORE it starts, because applying the update restarts the app --
an ack sent afterwards is an ack that never arrives, and the admin console
shows the machine as never having responded.

Reuses the connect-loop harness. No sockets, no restarts, no git.
"""
from __future__ import annotations

import asyncio

import pytest

# Captured at import. `_one_connection` patches asyncio.sleep globally to
# raise its stop sentinel, so a plain `await asyncio.sleep(0)` inside a test
# ends the test rather than yielding to the task under inspection.
_REAL_SLEEP = asyncio.sleep

from backend.src.services.cluster.remote import client as rc
from backend.src.services.cluster.remote.protocol import (
    MSG_GIT_UPDATE, MSG_UPDATE_STATUS, MSG_VERSION_INFO, MSG_WELCOME,
)

from tests.remote.test_client_connect_loop import (  # noqa: F401
    _one_connection, _run, _Ws, env, restarts, store, token_file,
)

WELCOME = {"type": MSG_WELCOME}
pytestmark = pytest.mark.asyncio


@pytest.fixture
def version(monkeypatch):
    monkeypatch.setattr(rc, "_app_version", lambda: "1.2.3")


class TestBeingToldAboutAVersion:
    async def test_a_newer_version_is_flagged(self, env, restarts, version,
                                              monkeypatch):
        _one_connection(monkeypatch, _Ws([WELCOME, {
            "type": MSG_VERSION_INFO, "latest": "1.3.0", "changelog": ["a"]}]))

        await _run()

        assert rc._status["update_available"] is True
        assert rc._status["latest_version"] == "1.3.0"

    async def test_the_same_version_is_not_flagged(self, env, restarts, version,
                                                   monkeypatch):
        """A machine already current must not be nagged to update to itself."""
        _one_connection(monkeypatch, _Ws([WELCOME, {
            "type": MSG_VERSION_INFO, "latest": "1.2.3"}]))

        await _run()

        assert rc._status["update_available"] is False

    async def test_an_empty_latest_is_not_flagged(self, env, restarts, version,
                                                  monkeypatch):
        """A server that could not read its own version must not make every
        client think an update exists."""
        _one_connection(monkeypatch, _Ws([WELCOME, {
            "type": MSG_VERSION_INFO, "latest": ""}]))

        await _run()

        assert rc._status["update_available"] is False

    async def test_the_changelog_is_kept_for_the_dashboard(self, env, restarts,
                                                           version, monkeypatch):
        _one_connection(monkeypatch, _Ws([WELCOME, {
            "type": MSG_VERSION_INFO, "latest": "1.3.0",
            "changelog": ["fixed a thing", "and another"]}]))

        await _run()

        assert rc._status["changelog"] == ["fixed a thing", "and another"]

    async def test_a_version_info_with_no_changelog_is_survivable(
        self, env, restarts, version, monkeypatch,
    ):
        _one_connection(monkeypatch, _Ws([WELCOME, {
            "type": MSG_VERSION_INFO, "latest": "1.3.0"}]))

        await _run()

        assert rc._status["changelog"] == []


class TestBeingToldToUpdate:
    @pytest.fixture
    def applier(self, monkeypatch):
        started: list = []

        async def _apply():
            started.append(True)
        monkeypatch.setattr(rc, "_apply_git_update", _apply)
        return started

    async def test_it_acknowledges(self, env, restarts, applier, monkeypatch):
        ws = _Ws([WELCOME, {"type": MSG_GIT_UPDATE}])
        _one_connection(monkeypatch, ws)

        await _run()

        assert MSG_UPDATE_STATUS in ws.types()

    async def test_the_ack_says_it_is_applying(self, env, restarts, applier,
                                               monkeypatch):
        ws = _Ws([WELCOME, {"type": MSG_GIT_UPDATE}])
        _one_connection(monkeypatch, ws)

        await _run()

        status = [m for m in ws.sent if m.get("type") == MSG_UPDATE_STATUS][0]
        assert status["status"] == "applying"

    async def test_it_acknowledges_before_it_starts(self, env, restarts,
                                                     applier, monkeypatch):
        """Applying restarts the app. An ack sent afterwards never arrives,
        and the console shows a machine that ignored the order."""
        import pathlib

        src = pathlib.Path(rc.__file__).read_text(encoding="utf-8")
        block = src[src.index("elif t == MSG_GIT_UPDATE:"):]
        block = block[:block.index("elif t ==", 10)]

        assert block.index("MSG_UPDATE_STATUS") < block.index("_apply_git_update")

    async def test_the_update_is_actually_started(self, env, restarts, applier,
                                                  monkeypatch):
        ws = _Ws([WELCOME, {"type": MSG_GIT_UPDATE}])
        _one_connection(monkeypatch, ws)

        await _run()
        await _REAL_SLEEP(0)      # let the create_task'd applier run

        assert applier == [True]
