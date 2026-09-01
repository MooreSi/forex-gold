"""Three things the admin server does to every client at once.

None of them was tested, and each fails in a way nobody would notice:

  * **resign_all_licences()** runs on every startup and re-signs every stored
    licence with the current signing key. If it aborts on the first bad token,
    every client after it keeps a licence signed by a retired key -- and a key
    that no longer verifies sends that client to the activation screen.
  * **_ping_loop()** is the only thing that reaps a connection that died
    without closing. A leak here means the admin console lists machines that
    are not there, and `push_update` reports success for them.
  * **push_update()** triggers a git self-update on every connected client. If
    it stops at the first failure, the clients behind it silently never update.

Everything here is a dict and a fake socket. No client is contacted.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.remote import server as rs

pytestmark = pytest.mark.asyncio


class _Ws:
    def __init__(self, fails=False):
        self.sent: list = []
        self.fails = fails

    async def send(self, raw):
        if self.fails:
            raise ConnectionResetError("gone")
        self.sent.append(json.loads(raw))


@pytest.fixture
def clean(monkeypatch):
    """Module state, emptied and restored."""
    monkeypatch.setattr(rs, "_allowed_tokens", {})
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_kg_sign_fn", None)
    saves: list = []
    monkeypatch.setattr(rs, "_save_tokens", lambda: saves.append(True))
    return saves


class TestResigningLicencesOnStartup:
    async def test_without_a_signing_function_nothing_is_touched(self, clean):
        """An admin console with no keygen registered must not silently
        rewrite every licence it holds."""
        rs._allowed_tokens.update({"t1": {"machine_id": "M1", "licence_key": "OLD"}})

        out = rs.resign_all_licences()

        assert out == {"resigned": 0, "unchanged": 0, "skipped": 1}
        assert rs._allowed_tokens["t1"]["licence_key"] == "OLD"

    async def test_a_new_signature_replaces_the_stored_one(self, clean, monkeypatch):
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: f"NEW-{mid}")
        rs._allowed_tokens.update({"t1": {"machine_id": "M1", "licence_key": "OLD"}})

        out = rs.resign_all_licences()

        assert out["resigned"] == 1
        assert rs._allowed_tokens["t1"]["licence_key"] == "NEW-M1"

    async def test_an_unchanged_signature_is_not_rewritten(self, clean, monkeypatch):
        """The common case on every restart. Counting it as a re-sign would
        push a licence to every client every time the server started."""
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: "SAME")
        rs._allowed_tokens.update({"t1": {"machine_id": "M1", "licence_key": "SAME"}})

        out = rs.resign_all_licences()

        assert out == {"resigned": 0, "unchanged": 1, "skipped": 0}

    async def test_it_only_saves_when_something_changed(self, clean, monkeypatch):
        saves = clean
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: "SAME")
        rs._allowed_tokens.update({"t1": {"machine_id": "M1", "licence_key": "SAME"}})

        rs.resign_all_licences()

        assert saves == []

    async def test_a_token_with_no_machine_id_is_skipped(self, clean, monkeypatch):
        """There is nothing to sign against. Signing with an empty machine id
        would mint a licence valid on no machine at all."""
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: f"NEW-{mid}")
        rs._allowed_tokens.update({"t1": {"machine_id": "", "licence_key": "OLD"}})

        out = rs.resign_all_licences()

        assert out["skipped"] == 1
        assert rs._allowed_tokens["t1"]["licence_key"] == "OLD"

    async def test_one_failure_does_not_stop_the_rest(self, clean, monkeypatch):
        """The one that matters. Aborting here leaves every token after the
        bad one signed by a retired key, and those clients get sent to the
        activation screen on their next start."""
        def _sign(mid, exp):
            if mid == "BAD":
                raise RuntimeError("keygen said no")
            return f"NEW-{mid}"
        monkeypatch.setattr(rs, "_kg_sign_fn", _sign)
        rs._allowed_tokens.update({
            "t1": {"machine_id": "BAD", "licence_key": "OLD"},
            "t2": {"machine_id": "M2", "licence_key": "OLD"},
        })

        out = rs.resign_all_licences()

        assert out["resigned"] == 1 and out["skipped"] == 1
        assert rs._allowed_tokens["t2"]["licence_key"] == "NEW-M2"

    async def test_a_connected_client_is_sent_its_new_licence(self, clean,
                                                              monkeypatch):
        """Otherwise it keeps the retired key until it next reconnects."""
        pushed: list = []

        async def _send(ws, token):
            pushed.append(token)
        monkeypatch.setattr(rs, "_send_licence", _send)
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: f"NEW-{mid}")
        rs._allowed_tokens.update({"t1": {"machine_id": "M1", "licence_key": "OLD"}})
        rs._connected.update({"t1": {"ws": _Ws(), "info": {}}})

        rs.resign_all_licences()
        await asyncio.sleep(0)

        assert pushed == ["t1"]

    async def test_a_disconnected_client_is_not_pushed_to(self, clean, monkeypatch):
        """Negative control: the push is conditional on being connected."""
        pushed: list = []

        async def _send(ws, token):
            pushed.append(token)
        monkeypatch.setattr(rs, "_send_licence", _send)
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: f"NEW-{mid}")
        rs._allowed_tokens.update({"t1": {"machine_id": "M1", "licence_key": "OLD"}})

        rs.resign_all_licences()
        await asyncio.sleep(0)

        assert pushed == []


    async def test_no_signing_function_is_a_normal_state_not_an_error(
        self, clean, caplog,
    ):
        """An admin console with no keygen registered is ordinary, and this
        runs on every startup. Falling through to the per-token handler
        produces identical COUNTS -- calling None raises, which is caught and
        skipped -- so only the log tells the two apart, and a warning per
        licence on every start is how a real re-sign failure gets buried.

        Mutation found this: deleting the guard changed no assertion.
        """
        import logging

        rs._allowed_tokens.update({
            "t1": {"machine_id": "M1", "licence_key": "OLD"},
            "t2": {"machine_id": "M2", "licence_key": "OLD"},
        })

        with caplog.at_level(logging.WARNING):
            rs.resign_all_licences()

        assert "resign failed" not in caplog.text


class TestPushingAnUpdate:
    async def test_nothing_connected_reports_nothing_sent(self, clean):
        out = await rs.push_update()

        assert out["sent"] == 0

    async def test_every_connected_client_is_triggered(self, clean):
        a, b = _Ws(), _Ws()
        rs._connected.update({"t1": {"ws": a, "info": {"name": "mac"}},
                              "t2": {"ws": b, "info": {"name": "vps"}}})

        out = await rs.push_update()

        assert out["sent"] == 2
        assert a.sent and b.sent

    async def test_one_failure_does_not_stop_the_others(self, clean):
        """A client that dropped must not cost the update for everyone behind
        it in the dict."""
        dead, alive = _Ws(fails=True), _Ws()
        rs._connected.update({"t1": {"ws": dead, "info": {"name": "dead"}},
                              "t2": {"ws": alive, "info": {"name": "alive"}}})

        out = await rs.push_update()

        assert out["sent"] == 1
        assert alive.sent

    async def test_the_count_excludes_the_one_that_failed(self, clean):
        """Reporting it as sent would tell the admin every machine updated."""
        rs._connected.update({"t1": {"ws": _Ws(fails=True), "info": {"name": "dead"}}})

        out = await rs.push_update()

        assert out["sent"] == 0

    async def test_progress_is_reported_for_both_outcomes(self, clean):
        msgs: list = []
        rs._connected.update({"t1": {"ws": _Ws(fails=True), "info": {"name": "dead"}},
                              "t2": {"ws": _Ws(), "info": {"name": "alive"}}})

        await rs.push_update(progress_cb=msgs.append)

        assert any("alive" in m for m in msgs)
        assert any("Failed" in m for m in msgs)


class TestTheReaper:
    async def test_a_dead_connection_is_dropped(self, clean, monkeypatch):
        """Nothing else removes a socket that died without closing. Left in,
        the admin console lists a machine that is not there and push_update
        counts it."""
        slept: list = []

        async def _sleep(_s):
            slept.append(_s)
            if len(slept) > 1:
                raise asyncio.CancelledError
        monkeypatch.setattr(rs.asyncio, "sleep", _sleep)
        rs._connected.update({"t1": {"ws": _Ws(fails=True), "info": {}}})

        with pytest.raises(asyncio.CancelledError):
            await rs._ping_loop()

        assert "t1" not in rs._connected

    async def test_a_live_connection_is_kept(self, clean, monkeypatch):
        """Negative control: a reaper that drops everything would pass the
        test above and disconnect the whole estate every 30 seconds."""
        slept: list = []

        async def _sleep(_s):
            slept.append(_s)
            if len(slept) > 1:
                raise asyncio.CancelledError
        monkeypatch.setattr(rs.asyncio, "sleep", _sleep)
        live = _Ws()
        rs._connected.update({"t1": {"ws": live, "info": {}}})

        with pytest.raises(asyncio.CancelledError):
            await rs._ping_loop()

        assert "t1" in rs._connected
        assert live.sent
