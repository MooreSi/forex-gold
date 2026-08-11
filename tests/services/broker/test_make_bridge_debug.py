"""_make_bridge selection is unchanged — the debug seam has NOT shipped.

The FakeMT5Bridge exists and is fully tested, but wiring it into
runtime._make_bridge is a money-touching edit that requires Simon's
sign-off + a demo session (local-debug-mode 020). Until then this pins
that the selection logic is byte-for-byte today's: native when available
and enabled, HTTP client otherwise — and that a debug_mode config key
changes nothing yet.

When Simon's seam edit lands, `test_debug_config_selects_the_same_real_
classes_today` is the one test that legitimately changes (it becomes
"debug selects the fake") — say so in that commit.

No test here can reach a broker: bridges are constructed, never connected.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.src import runtime
from backend.src.services.broker.mt5_client import MT5BridgeClient
from backend.src.services.broker.mt5_native import NativeMT5Bridge


def test_native_selected_when_available_and_enabled():
    with patch.object(runtime, "_native_bridge_available", return_value=True):
        bridge = runtime._make_bridge({"mt5_native_bridge_enabled": True})
    assert isinstance(bridge, NativeMT5Bridge)


def test_http_client_selected_when_native_unavailable():
    with patch.object(runtime, "_native_bridge_available", return_value=False):
        bridge = runtime._make_bridge({"mt5_bridge_url": "http://localhost:9000"})
    assert isinstance(bridge, MT5BridgeClient)
    assert bridge.url == "http://localhost:9000"


def test_native_disabled_by_config_falls_back_to_http():
    with patch.object(runtime, "_native_bridge_available", return_value=True):
        bridge = runtime._make_bridge({"mt5_native_bridge_enabled": False,
                                       "mt5_bridge_url": ""})
    assert isinstance(bridge, MT5BridgeClient)


def test_debug_config_selects_the_same_real_classes_today():
    """debug_mode in the config changes NOTHING here yet — the fake-bridge
    wiring is Simon-gated and must not slip in as a side effect."""
    with patch.object(runtime, "_native_bridge_available", return_value=False):
        bridge = runtime._make_bridge({"debug_mode": True, "mt5_bridge_url": ""})
    assert isinstance(bridge, MT5BridgeClient)
