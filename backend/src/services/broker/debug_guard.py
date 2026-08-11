"""Debug mode must never coexist with a real MT5 bridge.

Until the fake-bridge seam lands (stage2 phase5/010, Simon-gated),
`runtime._make_bridge` still selects the real classes — on a machine with
MT5 + credentials, a "debug" boot would log into the live account behind a
banner promising no real orders (review 2026-08-11, C1). This guard refuses
that boot loudly. Bridge *selection* stays untouched: the runtime passes
whatever `_make_bridge` chose through here unchanged.

Pinned by tests/runtime/test_debug_bridge_guard.py, including the control
that a FakeMT5Bridge boot is acceptable — when the seam lands, debug mode
selects the fake and this guard simply never trips.
"""
from __future__ import annotations

from backend.src.services.broker.mt5_client import MT5BridgeClient
from backend.src.services.broker.mt5_native import NativeMT5Bridge


def reject_real_bridge_in_debug(bridge, config: dict):
    """Return *bridge* unchanged, unless debug mode was handed a real one."""
    if config.get("debug_mode") and isinstance(bridge, (NativeMT5Bridge, MT5BridgeClient)):
        raise RuntimeError(
            "debug_mode is on but a real MT5 bridge was selected "
            f"({type(bridge).__name__}) — refusing to boot. Debug mode must "
            "never be able to reach a live account; the fake-bridge seam has "
            "not landed yet (stage2 phase5/010)."
        )
    return bridge
