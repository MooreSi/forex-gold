"""Debug mode must refuse to boot with a real MT5 bridge (review 2026-08-11, C1).

FOREX_DEBUG_MODE shows a banner promising "no real orders", but until the
Simon-gated fake-bridge seam lands, `_make_bridge` still builds the real
NativeMT5Bridge / MT5BridgeClient — on a machine with MT5 + credentials,
debug mode logs into the live account with a fresh debug DB whose halts are
all default-off.

The guard pinned here is NOT the Simon-gated seam edit: bridge *selection* in
`_make_bridge` stays byte-identical (pinned by
tests/services/broker/test_make_bridge_debug.py). This is a refusal at
runtime construction — debug mode and a real bridge must never coexist, so
boot fails loudly instead of trading a live account behind a "debug" banner.

When the seam later lands (debug selects FakeMT5Bridge), the two refusal
tests here can no longer arrange debug + a real bridge through config alone;
that commit legitimately reworks them into direct guard tests — say so in it.

No test in this file can reach a broker: bridges are constructed, never
connected — no `.startup()`, no login, no order call (the same discipline as
test_make_bridge_debug.py, whose docstring documents that construction is
inert).
"""
from __future__ import annotations

from unittest.mock import patch

import pytest

from backend.src import runtime
from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.broker.mt5_client import MT5BridgeClient

_FIXED_TS = 1_700_000_000.0  # rule 12: FakeMT5Bridge must not read the wall clock


def test_debug_boot_refuses_a_native_real_bridge():
    """The exact live-account trap: debug on, MT5 importable, native enabled."""
    with patch.object(runtime, "_native_bridge_available", return_value=True):
        with pytest.raises(RuntimeError, match="[Dd]ebug"):
            runtime.TradingRuntime({"debug_mode": True,
                                    "mt5_native_bridge_enabled": True})


def test_debug_boot_refuses_the_http_real_bridge():
    """The HTTP client is just as real — it fronts the live bridge process."""
    with patch.object(runtime, "_native_bridge_available", return_value=False):
        with pytest.raises(RuntimeError, match="[Dd]ebug"):
            runtime.TradingRuntime({"debug_mode": True,
                                    "mt5_bridge_url": "http://localhost:9000"})


def test_debug_off_still_boots_the_real_bridge_as_today():
    """Negative control: the guard must not touch the real, non-debug boot."""
    with patch.object(runtime, "_native_bridge_available", return_value=False):
        rt = runtime.TradingRuntime({"debug_mode": False, "mt5_bridge_url": ""})
    assert isinstance(rt._bridge, MT5BridgeClient)


def test_a_fake_bridge_is_acceptable_in_debug_mode():
    """Control for the future seam: the guard refuses REAL bridges, not debug
    mode itself — a FakeMT5Bridge boot must construct cleanly."""
    fake = FakeMT5Bridge(base_ts=_FIXED_TS, clock=lambda: _FIXED_TS)
    with patch.object(runtime, "_make_bridge", return_value=fake):
        rt = runtime.TradingRuntime({"debug_mode": True})
    assert rt._bridge is fake
