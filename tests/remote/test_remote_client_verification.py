"""Who the admin client is willing to talk to, and when it decides.

Stage 2 of bugs/014. The hello this client sends carries the licence token,
the machine UUID and the hostname (`_build_hello`), so the peer has to be
established BEFORE it goes out, not after.

Two paths, two mechanisms, on the owner's instruction (2026-09-01):

  * **the internet path** (`SERVER_HOST`) verifies against a private CA
    bundled in the app. When no CA is bundled -- every build before the
    cutover -- it behaves exactly as it does today, because enforcing
    verification against a server that still presents a self-signed
    certificate would lock every client out of the machine they would need to
    fix it from.
  * **the LAN path** (a beacon-discovered address) trust-on-first-use pins,
    because no certificate for the public address can validate a local one and
    the local address varies by network.

**There is deliberately no TOFU on the internet path.** Pinning the current
self-signed certificate there would mean that the day the CA-signed
certificate is deployed, every already-updated client sees a mismatch and
refuses -- a lockout created by the upgrade itself. That path goes straight
from unauthenticated to CA-verified in one build, with no pinning stage
between.
"""
from __future__ import annotations

import ssl

import pytest

from backend.src.services.cluster.remote import ca as remote_ca
from backend.src.services.cluster.remote import tls as remote_tls

WAN = remote_tls.SERVER_HOST
LAN = "192.168.1.50"


@pytest.fixture
def bundled(tmp_path, monkeypatch):
    """A CA bundled in the build, as stage 3 will ship it."""
    ca_cert, ca_key = remote_ca.init_ca(tmp_path / "ca")
    monkeypatch.setattr(remote_ca, "bundled_ca_path", lambda: ca_cert)
    return ca_cert, ca_key


@pytest.fixture
def not_bundled(monkeypatch):
    """Every build before the cutover."""
    monkeypatch.setattr(remote_ca, "bundled_ca_path", lambda: None)


class TestTheInternetPath:
    def test_it_verifies_against_the_bundled_authority(self, bundled):
        ctx = remote_tls.client_ssl_context(WAN)

        assert ctx.verify_mode == ssl.CERT_REQUIRED
        assert ctx.check_hostname is True

    def test_without_a_bundled_authority_nothing_changes(self, not_bundled):
        """No regression on builds made before the cutover. This is today's
        behaviour, and it is why the rollout is safe to ship ahead of the
        server."""
        ctx = remote_tls.client_ssl_context(WAN)

        assert ctx.verify_mode == ssl.CERT_NONE

    def test_a_ca_verified_connection_needs_no_further_check(self, bundled):
        """TLS already established who the peer is. Asking for a fingerprint
        match on top would reintroduce exactly the pin-mismatch lockout the CA
        design exists to avoid."""
        assert remote_tls.is_ca_verified(WAN) is True

    def test_it_is_not_ca_verified_without_a_bundle(self, not_bundled):
        assert remote_tls.is_ca_verified(WAN) is False


class TestTheLanPath:
    def test_it_does_not_use_the_authority_even_when_one_is_bundled(self, bundled):
        """A certificate for the public address cannot validate a local one,
        and the local address changes with the network."""
        ctx = remote_tls.client_ssl_context(LAN)

        assert ctx.verify_mode == ssl.CERT_NONE

    def test_it_is_never_ca_verified(self, bundled):
        assert remote_tls.is_ca_verified(LAN) is False

    def test_localhost_is_treated_as_lan(self, bundled):
        assert remote_tls.is_ca_verified("127.0.0.1") is False


class TestDecidingWhetherToSendTheToken:
    def test_a_ca_verified_peer_is_accepted(self, bundled):
        ok, why = remote_tls.peer_is_acceptable(WAN, presented="")

        assert ok is True
        assert "authority" in why.lower() or "verified" in why.lower()

    def test_a_ca_verified_peer_does_not_need_a_fingerprint(self, bundled):
        """The fingerprint is unreadable on some transports. On the CA path
        that must not matter -- requiring it would refuse a connection TLS has
        already authenticated."""
        ok, _ = remote_tls.peer_is_acceptable(WAN, presented="")

        assert ok is True

    def test_a_lan_peer_with_no_readable_certificate_is_refused(self, bundled, fresh_db):
        """Nothing to pin and nothing to compare. Refusing is the only safe
        answer, because the next thing sent is the token."""
        ok, why = remote_tls.peer_is_acceptable(LAN, presented="")

        assert ok is False
        assert "certificate" in why.lower()

    def test_a_lan_peer_is_pinned_on_first_sight(self, fresh_db, not_bundled):
        ok, why = remote_tls.peer_is_acceptable(LAN, presented="AA:BB")

        assert ok is True
        assert "first" in why.lower()

    def test_the_same_lan_peer_is_accepted_again(self, fresh_db, not_bundled):
        remote_tls.peer_is_acceptable(LAN, presented="AA:BB")

        ok, _ = remote_tls.peer_is_acceptable(LAN, presented="AA:BB")

        assert ok is True

    def test_a_changed_lan_certificate_is_refused(self, fresh_db, not_bundled):
        remote_tls.peer_is_acceptable(LAN, presented="AA:BB")

        ok, why = remote_tls.peer_is_acceptable(LAN, presented="CC:DD")

        assert ok is False
        assert "AA:BB" in why and "CC:DD" in why

    def test_a_refusal_does_not_overwrite_the_pin(self, fresh_db, not_bundled):
        """Otherwise a second attempt from the same wrong peer succeeds."""
        remote_tls.peer_is_acceptable(LAN, presented="AA:BB")
        remote_tls.peer_is_acceptable(LAN, presented="CC:DD")

        ok, _ = remote_tls.peer_is_acceptable(LAN, presented="CC:DD")

        assert ok is False

    def test_the_refusal_says_how_to_recover(self, fresh_db, not_bundled):
        """A lockout with no stated way out is how an admin loses a machine."""
        remote_tls.peer_is_acceptable(LAN, presented="AA:BB")

        _ok, why = remote_tls.peer_is_acceptable(LAN, presented="CC:DD")

        assert "clear" in why.lower() or "re-pair" in why.lower() or "pair" in why.lower()


class TestTheOrdering:
    def test_the_token_is_not_sent_before_the_peer_is_checked(self):
        """Structural, and the property the whole change exists for. The
        equivalent for the sync channel is pinned the same way -- see
        tests/core/test_sync_cert_pinning.py.
        """
        import pathlib

        from backend.src.services.cluster.remote import client as rc

        src = pathlib.Path(rc.__file__).read_text(encoding="utf-8")
        loop = src[src.index("async def _connect_loop"):]

        check = loop.index("peer_is_acceptable")
        hello = loop.index("_build_hello()")

        assert check < hello, (
            "the hello -- which carries the licence token, the machine UUID "
            "and the hostname -- is sent before the peer is established"
        )
