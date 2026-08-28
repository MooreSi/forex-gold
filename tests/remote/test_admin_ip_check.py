"""External-IP detection and the admin-machine check.

is_admin_machine() is one of the gates on remote admin authority, and it
depends on an outbound HTTP call that can fail, be slow, or answer with
rubbish. What matters is that every one of those outcomes denies.

The cache is the subtle part: a five-minute TTL on a security check means a
wrong answer persists, so the tests pin what is cached and when it is not.

No network. httpx is faked for every test, and the module-level cache is reset
around each one so ordering cannot leak an answer between them.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.remote import ip_check


@pytest.fixture(autouse=True)
def clean_cache():
    ip_check.invalidate_cache()
    yield
    ip_check.invalidate_cache()


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeClient:
    """Stands in for httpx.AsyncClient. Records which services were tried."""

    calls: list = []
    behaviour: dict = {}

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def get(self, url):
        _FakeClient.calls.append(url)
        result = _FakeClient.behaviour.get(url, "")
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)


@pytest.fixture
def fake_httpx(monkeypatch):
    import httpx
    _FakeClient.calls = []
    _FakeClient.behaviour = {}
    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    return _FakeClient


def _ip():
    return asyncio.run(ip_check.get_external_ip())


class TestFetching:
    def test_the_first_service_that_answers_wins(self, fake_httpx):
        fake_httpx.behaviour = {ip_check._IP_SERVICES[0]: "1.2.3.4"}
        assert _ip() == "1.2.3.4"
        assert fake_httpx.calls == [ip_check._IP_SERVICES[0]], "it should stop at the first"

    def test_it_falls_through_to_the_next_service_on_failure(self, fake_httpx):
        fake_httpx.behaviour = {
            ip_check._IP_SERVICES[0]: RuntimeError("connection refused"),
            ip_check._IP_SERVICES[1]: "5.6.7.8",
        }
        assert _ip() == "5.6.7.8"

    def test_an_empty_answer_is_not_accepted_as_an_ip(self, fake_httpx):
        """A service returning "" must not be treated as a successful answer,
        or a blank IP gets cached for five minutes."""
        fake_httpx.behaviour = {
            ip_check._IP_SERVICES[0]: "",
            ip_check._IP_SERVICES[1]: "5.6.7.8",
        }
        assert _ip() == "5.6.7.8"

    def test_the_answer_is_stripped(self, fake_httpx):
        """checkip.amazonaws.com returns a trailing newline. Comparing an
        unstripped "1.2.3.4\\n" against the admin IP fails silently."""
        fake_httpx.behaviour = {ip_check._IP_SERVICES[0]: "  1.2.3.4\n"}
        assert _ip() == "1.2.3.4"

    def test_every_service_failing_yields_empty(self, fake_httpx):
        fake_httpx.behaviour = {s: RuntimeError("down") for s in ip_check._IP_SERVICES}
        assert _ip() == ""
        assert len(fake_httpx.calls) == len(ip_check._IP_SERVICES), "all should be tried"


class TestCaching:
    def test_a_successful_answer_is_cached(self, fake_httpx):
        fake_httpx.behaviour = {ip_check._IP_SERVICES[0]: "1.2.3.4"}
        _ip()
        _ip()
        assert len(fake_httpx.calls) == 1, "the second call should not hit the network"

    def test_a_failure_is_not_cached(self, fake_httpx):
        """Caching "" would keep the machine locked out for five minutes after
        one transient network blip."""
        fake_httpx.behaviour = {s: RuntimeError("down") for s in ip_check._IP_SERVICES}
        _ip()
        first_round = len(fake_httpx.calls)
        _ip()
        assert len(fake_httpx.calls) > first_round, "a failed lookup must be retried"

    def test_invalidate_cache_forces_a_refetch(self, fake_httpx):
        fake_httpx.behaviour = {ip_check._IP_SERVICES[0]: "1.2.3.4"}
        _ip()
        ip_check.invalidate_cache()
        _ip()
        assert len(fake_httpx.calls) == 2


class TestTheAdminGate:
    """Every case here except the exact match must be False. A bug that makes
    any of them True hands admin authority to the wrong machine."""

    def test_the_configured_admin_ip_passes(self, fake_httpx):
        fake_httpx.behaviour = {ip_check._IP_SERVICES[0]: ip_check.ADMIN_EXTERNAL_IP}
        assert asyncio.run(ip_check.is_admin_machine()) is True

    def test_any_other_ip_is_refused(self, fake_httpx):
        fake_httpx.behaviour = {ip_check._IP_SERVICES[0]: "9.9.9.9"}
        assert asyncio.run(ip_check.is_admin_machine()) is False

    def test_a_prefix_of_the_admin_ip_is_refused(self, fake_httpx):
        """Substring rather than equality would let 217.155.25.16 through."""
        fake_httpx.behaviour = {
            ip_check._IP_SERVICES[0]: ip_check.ADMIN_EXTERNAL_IP[:-1]}
        assert asyncio.run(ip_check.is_admin_machine()) is False

    def test_no_answer_at_all_is_refused(self, fake_httpx):
        """The failure mode that matters most: the lookup could not run, so
        the machine is NOT proven to be the admin's."""
        fake_httpx.behaviour = {s: RuntimeError("down") for s in ip_check._IP_SERVICES}
        assert asyncio.run(ip_check.is_admin_machine()) is False


class TestMachineUuid:
    def test_non_mac_returns_empty_rather_than_raising(self, monkeypatch):
        """Windows is a supported platform for this app; ioreg does not exist
        there and the caller expects '' rather than an exception."""
        import sys
        monkeypatch.setattr(sys, "platform", "win32")
        assert ip_check.get_machine_uuid() == ""

    def test_a_failing_lookup_returns_empty(self, monkeypatch):
        import subprocess, sys
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(subprocess, "check_output",
                            lambda *a, **k: (_ for _ in ()).throw(OSError("no ioreg")))
        assert ip_check.get_machine_uuid() == ""

    def test_it_parses_the_uuid_out_of_ioreg_output(self, monkeypatch):
        import subprocess, sys
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(subprocess, "check_output", lambda *a, **k: (
            '  +-o IOPlatformExpertDevice  <class IOPlatformExpertDevice>\n'
            '      "IOPlatformUUID" = "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"\n'
            '      "model" = <"Macmini9,1">\n'
        ))
        assert ip_check.get_machine_uuid() == "AAAAAAAA-BBBB-CCCC-DDDD-EEEEEEEEEEEE"

    def test_output_without_a_uuid_line_returns_empty(self, monkeypatch):
        import subprocess, sys
        monkeypatch.setattr(sys, "platform", "darwin")
        monkeypatch.setattr(subprocess, "check_output",
                            lambda *a, **k: '      "model" = <"Macmini9,1">\n')
        assert ip_check.get_machine_uuid() == ""
