"""FakeMT5Bridge matches the real bridge clients' duck-typed surface.

The runtime never type-checks its bridge — everything above `_bridge` calls
these methods by name. A fake missing one method, or with a drifted
signature, fails at 3am in a code path nobody exercised. This test
introspects BOTH real clients and asserts the fake carries every public
method with the same parameters.

No test in this file can reach a broker: only signatures are inspected;
nothing is connected, and the fake has no network code at all.
"""
from __future__ import annotations

import inspect

from backend.src.services.broker.fake_bridge import FakeMT5Bridge
from backend.src.services.broker.mt5_client import MT5BridgeClient
from backend.src.services.broker.mt5_native import NativeMT5Bridge


def _public_methods(cls) -> dict[str, inspect.Signature]:
    return {
        name: inspect.signature(fn)
        for name, fn in inspect.getmembers(cls, inspect.isfunction)
        if not name.startswith("_")
    }


def _surface_gaps(real_cls, fake_cls) -> list[str]:
    """Every public method of the real client must exist on the fake with the
    same parameter names (self included) and async-ness."""
    gaps = []
    fake = _public_methods(fake_cls)
    for name, sig in _public_methods(real_cls).items():
        if name not in fake:
            gaps.append(f"missing: {name}")
            continue
        real_params = list(sig.parameters)
        fake_params = list(fake[name].parameters)
        if real_params != fake_params:
            gaps.append(f"signature drift on {name}: {real_params} != {fake_params}")
        if inspect.iscoroutinefunction(getattr(real_cls, name)) != \
                inspect.iscoroutinefunction(getattr(fake_cls, name)):
            gaps.append(f"async mismatch on {name}")
    return gaps


def test_surface_matches_the_http_client():
    assert _surface_gaps(MT5BridgeClient, FakeMT5Bridge) == []


def test_surface_matches_the_native_client():
    assert _surface_gaps(NativeMT5Bridge, FakeMT5Bridge) == []


def test_surface_check_can_fail():
    """Negative control: the checker sees a missing method and a drifted
    signature."""

    class _Impostor:
        def is_configured(self, wrong_extra_arg) -> bool:  # drifted signature
            return True
        # everything else missing

    gaps = _surface_gaps(MT5BridgeClient, _Impostor)
    assert any(g.startswith("missing: place_order") for g in gaps)
    assert any("is_configured" in g and "drift" in g for g in gaps)


def test_fake_has_url_and_is_configured():
    fake = FakeMT5Bridge()
    assert isinstance(fake.url, str) and fake.url
    assert fake.is_configured() is True
