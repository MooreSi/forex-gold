"""The bridge status reads the port the bridge is actually configured on.

Owner report, 2026-09-02: "MT5 Bridge Control ... says Bridge: NOT running".

It was checking a hardcoded 9000 while the configured `mt5_bridge_url` was
`http://localhost:9010`. The bridge was up and healthy the whole time --
`GET /health` returned `{"status":"connected","trade_allowed":true}` -- so the
label was wrong, not the bridge.

The wrong label is the harmless half. `start_bridge()` guards with
`if _bridge_running(): "already running"`, so with the check pointed at a port
nothing uses, pressing Start Bridge would have launched a SECOND bridge
against the same MT5 terminal.
"""
from __future__ import annotations

import pytest

from frontend.pages.settings import _bridge


@pytest.fixture
def port_seen(monkeypatch):
    seen: list = []
    monkeypatch.setattr(_bridge._pu, "is_port_listening",
                        lambda p: seen.append(p) or False)
    return seen


def _url(monkeypatch, value):
    monkeypatch.setattr(_bridge, "_bridge_url", lambda: value)


class TestThePortComesFromTheConfiguredUrl:
    def test_a_custom_port_is_used(self, monkeypatch, port_seen):
        _url(monkeypatch, "http://localhost:9010")

        _bridge._bridge_running()

        assert port_seen == [9010]

    def test_the_default_port_still_works(self, monkeypatch, port_seen):
        _url(monkeypatch, "http://localhost:9000")

        _bridge._bridge_running()

        assert port_seen == [9000]

    def test_a_url_with_no_port_falls_back_to_9000(self, monkeypatch, port_seen):
        """http:// with no port means 80, but this app has always meant the
        bridge default. Guessing 80 would report "not running" for ever."""
        _url(monkeypatch, "http://localhost")

        _bridge._bridge_running()

        assert port_seen == [9000]

    def test_a_blank_url_falls_back_to_9000(self, monkeypatch, port_seen):
        _url(monkeypatch, "")

        _bridge._bridge_running()

        assert port_seen == [9000]

    @pytest.mark.parametrize("bad", [
        "not a url at all",     # urlparse tolerates this: .port is just None
        "http://localhost:abc",   # .port RAISES ValueError
        "http://localhost:99999",  # out of range -- also raises
    ])
    def test_rubbish_does_not_raise(self, monkeypatch, port_seen, bad):
        """This runs on every settings-page refresh, so a traceback here takes
        the page down with it.

        The three cases are not interchangeable: `urlparse` returns None for
        gibberish but RAISES on a malformed or out-of-range port, and only the
        raising ones exercise the guard. The first version of this test used
        gibberish alone and a mutant that deleted the try/except survived it.
        """
        _url(monkeypatch, bad)

        _bridge._bridge_running()

        assert port_seen == [9000]

    def test_a_remote_bridge_host_still_reads_its_port(self, monkeypatch,
                                                       port_seen):
        _url(monkeypatch, "http://192.168.0.50:9100")

        _bridge._bridge_running()

        assert port_seen == [9100]


class TestItIsNotHardcoded:
    def test_the_source_no_longer_pins_9000_in_the_check(self):
        import pathlib

        src = pathlib.Path(_bridge.__file__).read_text(encoding="utf-8")
        body = src[src.index("def _bridge_running("):]
        body = body[:body.index("\n\n\n")]
        code = "\n".join(ln for ln in body.splitlines()
                         if not ln.strip().startswith("#"))

        assert "is_port_listening(9000)" not in code
