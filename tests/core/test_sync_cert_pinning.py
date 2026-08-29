"""Certificate pinning for the sync channel (bugs/014).

Before this, both cluster channels were encrypted but UNAUTHENTICATED. The
client context sets check_hostname=False and verify_mode=CERT_NONE, which is
the normal pattern for a self-signed server on a bare IP -- but it is only safe
if the client then compares the presented certificate against a fingerprint it
already knows. Nothing did. `tls_util`'s docstring claimed
`client.py::_verify_fingerprint` handled it; that function never existed.

It mattered because `_connect_once` sends the shared token on the FIRST frame
after the handshake. The token authenticates the client to the server; nothing
authenticated the server to the client, so anyone able to intercept the
connection could present any certificate and be handed the token.

The rule now:

  FIRST CONNECTION  nothing is pinned yet, so the presented fingerprint is
                    stored and accepted. Trust-on-first-use. This is what keeps
                    an already-paired Mac/VPS working across the upgrade -- it
                    pins whatever it is already talking to.
  AFTERWARDS        a mismatch is refused BEFORE the token is sent, and stays
                    refused. A reconnect loop that quietly retried would turn
                    an interception into "the sync is flaky".

The first connection is still exposed. That is the known limit of TOFU and is
recorded in 014 rather than papered over.
"""
from __future__ import annotations

import ssl

import pytest

from backend.src.services.cluster.sync import tls_util


pytestmark = pytest.mark.usefixtures("fresh_db")


@pytest.fixture(autouse=True)
def isolated_paths(monkeypatch, tmp_path):
    d = tmp_path / "sync"
    monkeypatch.setattr(tls_util, "_SYNC_DIR", d)
    monkeypatch.setattr(tls_util, "_CERT_FILE", d / "sync_cert.pem")
    monkeypatch.setattr(tls_util, "_KEY_FILE", d / "sync_key.pem")
    monkeypatch.setattr(tls_util, "_FPRINT_FILE", d / "sync_cert.fingerprint")
    return d


class _Reached(Exception):
    """Raised by the fake peer once the token has gone out, so a test can
    assert the connect path got that far without needing a real server."""


FP_A = ":".join(["aa"] * 32)
FP_B = ":".join(["bb"] * 32)
HOST = "203.0.113.10"


class TestTrustOnFirstUse:
    def test_the_first_fingerprint_is_accepted(self):
        ok, _ = tls_util.verify_or_pin(HOST, FP_A)
        assert ok is True

    def test_and_remembered(self):
        tls_util.verify_or_pin(HOST, FP_A)
        assert tls_util.pinned_fingerprint(HOST) == FP_A

    def test_it_survives_a_restart(self):
        """Stored in app_config, not memory. A pin that vanished on restart
        would re-trust whatever answered next time."""
        tls_util.verify_or_pin(HOST, FP_A)
        assert tls_util.pinned_fingerprint(HOST) == FP_A

    def test_nothing_is_pinned_before_the_first_connection(self):
        assert tls_util.pinned_fingerprint(HOST) == ""


class TestAfterwards:
    def test_the_same_fingerprint_is_accepted(self):
        tls_util.verify_or_pin(HOST, FP_A)
        ok, _ = tls_util.verify_or_pin(HOST, FP_A)
        assert ok is True

    def test_A_DIFFERENT_FINGERPRINT_IS_REFUSED(self):
        """The whole point."""
        tls_util.verify_or_pin(HOST, FP_A)

        ok, reason = tls_util.verify_or_pin(HOST, FP_B)

        assert ok is False
        assert FP_B[:20] in reason or "match" in reason.lower()

    def test_a_refusal_does_NOT_overwrite_the_pin(self):
        """Otherwise the second attempt from the same attacker succeeds."""
        tls_util.verify_or_pin(HOST, FP_A)

        tls_util.verify_or_pin(HOST, FP_B)

        assert tls_util.pinned_fingerprint(HOST) == FP_A
        assert tls_util.verify_or_pin(HOST, FP_B)[0] is False

    def test_an_empty_presented_fingerprint_is_refused(self):
        """No certificate read means no verification happened. Treating that
        as a pass would restore the original hole exactly."""
        tls_util.verify_or_pin(HOST, FP_A)
        assert tls_util.verify_or_pin(HOST, "")[0] is False

    def test_an_empty_fingerprint_is_refused_even_with_NOTHING_pinned(self):
        """It must not be pinned as the empty string either -- that would
        match every future failure to read a certificate."""
        ok, _ = tls_util.verify_or_pin(HOST, "")

        assert ok is False
        assert tls_util.pinned_fingerprint(HOST) == ""


class TestPinsArePerHost:
    def test_a_different_host_pins_separately(self):
        """Moving the VPS to a new address is a new pairing, not a mismatch."""
        tls_util.verify_or_pin(HOST, FP_A)

        ok, _ = tls_util.verify_or_pin("198.51.100.7", FP_B)

        assert ok is True
        assert tls_util.pinned_fingerprint(HOST) == FP_A
        assert tls_util.pinned_fingerprint("198.51.100.7") == FP_B


class TestRepairing:
    def test_clearing_the_pin_allows_a_new_one(self):
        """The recovery path for a genuinely reissued certificate. Without
        one, a legitimately rotated cert locks the user out permanently."""
        tls_util.verify_or_pin(HOST, FP_A)

        tls_util.clear_pin(HOST)

        assert tls_util.pinned_fingerprint(HOST) == ""
        assert tls_util.verify_or_pin(HOST, FP_B)[0] is True

    def test_clearing_one_host_leaves_another_alone(self):
        tls_util.verify_or_pin(HOST, FP_A)
        tls_util.verify_or_pin("198.51.100.7", FP_B)

        tls_util.clear_pin(HOST)

        assert tls_util.pinned_fingerprint("198.51.100.7") == FP_B

    def test_reconfiguring_the_link_clears_the_pin(self):
        """Re-entering host/port/token in Settings > Remote Node is the user
        saying "pair with this VPS again". That is the discoverable recovery
        route, so it must not leave a stale pin behind that refuses the very
        server they just pointed at."""
        from backend.src.services.cluster.sync.client import SyncClient

        tls_util.verify_or_pin(HOST, FP_A)

        SyncClient().configure(HOST, 9001, "a-token")

        assert tls_util.pinned_fingerprint(HOST) == ""


class TestReadingTheFingerprintOffARealHandshake:
    """Not a mock. The extraction path (ws.transport -> ssl_object ->
    getpeercert) is a websockets internal, so it is proved against a real TLS
    handshake using this app's own server context. A mocked test here would
    pass while the real attribute path was wrong -- which is the failure mode
    that produced 014 in the first place."""

    @pytest.mark.asyncio
    async def test_the_fingerprint_matches_the_servers_own_certificate(self):
        import asyncio
        import websockets

        tls_util.ensure_cert("127.0.0.1")
        expected = tls_util.cert_fingerprint()
        assert expected, "no certificate was generated"

        seen: dict = {}

        async def _handler(ws):
            await ws.recv()

        server = await websockets.serve(
            _handler, "127.0.0.1", 0,
            ssl=tls_util.server_ssl_context("127.0.0.1"))
        port = server.sockets[0].getsockname()[1]
        try:
            async with websockets.connect(
                f"wss://127.0.0.1:{port}",
                ssl=tls_util.client_ssl_context(),
            ) as ws:
                seen["fp"] = tls_util.peer_fingerprint(ws)
                await ws.send("hi")
        finally:
            server.close()
            await server.wait_closed()

        assert seen["fp"] == expected, (
            "the fingerprint read from the live connection does not match the "
            "certificate the server actually presented")

    def test_it_returns_empty_rather_than_raising_on_a_non_tls_object(self):
        """Called on the connect path. An exception there would break the
        link instead of refusing it, and the refusal is the safe outcome."""
        class _NotAWebsocket:
            pass

        assert tls_util.peer_fingerprint(_NotAWebsocket()) == ""


class TestTheTokenIsNotSentToAnUnverifiedPeer:
    """The actual vulnerability in 014, and the only tests here that prove it
    is closed. Pinning that happens AFTER the token leaves is worth nothing --
    the attacker already has it."""

    @staticmethod
    def _install_fake_websockets(monkeypatch, presented, sent):
        import contextlib

        class _Ws:
            transport = None

            async def send(self, raw):
                sent.append(raw)

            async def recv(self):
                raise _Reached("the token was sent")

            async def close(self):
                pass

        @contextlib.asynccontextmanager
        async def _fake_connect(uri, **kw):
            yield _Ws()

        # Patch websockets.connect itself rather than swapping the module in
        # sys.modules -- a module-level swap leaked into unrelated tests when
        # the whole suite ran, and a fake module missing serve()/exceptions is
        # a trap for anything that imports websockets afterwards.
        import websockets
        monkeypatch.setattr(websockets, "connect", _fake_connect)
        monkeypatch.setattr(tls_util, "peer_fingerprint", lambda ws: presented)

    @pytest.mark.asyncio
    async def test_a_mismatched_certificate_gets_NO_TOKEN(self, monkeypatch):
        from backend.src.services.cluster.sync.client import SyncClient
        from backend.src.services.cluster.sync.protocol import CONN_REJECTED

        tls_util.verify_or_pin(HOST, FP_A)          # already paired
        sent: list = []
        self._install_fake_websockets(monkeypatch, FP_B, sent)   # someone else

        client = SyncClient()
        client._host, client._port = HOST, 9001
        client._token = "the-shared-secret"

        await client._connect_once()

        assert sent == [], "the shared token was sent to an unverified peer"
        assert client.conn_state == CONN_REJECTED

    @pytest.mark.asyncio
    async def test_the_pin_is_not_overwritten_by_the_impostor(self, monkeypatch):
        from backend.src.services.cluster.sync.client import SyncClient

        tls_util.verify_or_pin(HOST, FP_A)
        sent: list = []
        self._install_fake_websockets(monkeypatch, FP_B, sent)

        client = SyncClient()
        client._host, client._port = HOST, 9001
        client._token = "the-shared-secret"
        await client._connect_once()

        assert tls_util.pinned_fingerprint(HOST) == FP_A

    @pytest.mark.asyncio
    async def test_a_matching_certificate_PROCEEDS_to_send_the_token(self, monkeypatch):
        from backend.src.services.cluster.sync.client import SyncClient

        tls_util.verify_or_pin(HOST, FP_A)
        sent: list = []
        self._install_fake_websockets(monkeypatch, FP_A, sent)

        client = SyncClient()
        client._host, client._port = HOST, 9001
        client._token = "the-shared-secret"

        with pytest.raises(_Reached):
            await client._connect_once()

        assert sent, "a verified peer was refused"
        assert "the-shared-secret" in sent[0]

    @pytest.mark.asyncio
    async def test_an_UNREADABLE_certificate_gets_no_token_either(self, monkeypatch):
        """peer_fingerprint returns "" when it cannot read the certificate.
        Treating that as a pass would restore the original hole exactly."""
        from backend.src.services.cluster.sync.client import SyncClient
        from backend.src.services.cluster.sync.protocol import CONN_REJECTED

        tls_util.verify_or_pin(HOST, FP_A)
        sent: list = []
        self._install_fake_websockets(monkeypatch, "", sent)

        client = SyncClient()
        client._host, client._port = HOST, 9001
        client._token = "the-shared-secret"

        await client._connect_once()

        assert sent == []
        assert client.conn_state == CONN_REJECTED
