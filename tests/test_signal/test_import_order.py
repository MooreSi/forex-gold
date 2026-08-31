"""Either module may be imported first.

`signal_generator` and `signal_indicators` depend on each other:
`signal_indicators` needs the candle helpers, and `signal_generator` re-exports
everything that moved into `signal_indicators` so existing callers keep
addressing `signal_generator.<name>`.

Until 2026-08-31 that cycle resolved in exactly ONE order. Importing
`signal_generator` first worked; importing `signal_indicators` first raised
`ImportError: cannot import name 'calculate_risk_levels' from partially
initialized module`. It never failed in production only because the engine
imports the generator early — so the whole thing rested on import order that
nothing declared and nothing checked.

That is the kind of trap that costs an afternoon: a new module imports the
indicators, and the app stops booting for a reason that has nothing to do with
the change.

Each import runs in a fresh subprocess, because once either module is in
`sys.modules` the order stops meaning anything.
"""
from __future__ import annotations

import subprocess
import sys

import pytest

_MODULES = [
    "backend.src.services.test_signal.signal_generator",
    "backend.src.services.test_signal.signal_indicators",
]

# Everything `signal_generator` re-exports on behalf of `signal_indicators`.
# Callers across breakout_signal and test_signal address these through the
# generator, so a lazy re-export that silently stopped resolving would break
# them at first use rather than at import.
_REEXPORTS = [
    "calculate_risk_levels", "calculate_scalp_risk_levels",
    "check_scalp_trigger", "compute_adx", "compute_h4_bias",
    "compute_macd_hist", "detect_regime", "_counter_bias_allowed",
]


def _run(code: str):
    return subprocess.run([sys.executable, "-c", code],
                          capture_output=True, text=True)


@pytest.mark.parametrize("first", _MODULES)
def test_either_module_can_be_imported_first(first):
    proc = _run(f"import {first}")

    assert proc.returncode == 0, (
        f"importing {first} first fails:\n{proc.stderr[-1500:]}"
    )


@pytest.mark.parametrize("name", _REEXPORTS)
def test_the_re_exports_still_resolve_through_the_generator(name):
    """The fix must not turn the re-exports into names that only exist on
    paper. These are how `breakout_signal` and `test_signal_generate` reach
    them."""
    proc = _run(
        "from backend.src.services.test_signal.signal_generator import "
        f"{name}; assert callable({name})"
    )

    assert proc.returncode == 0, proc.stderr[-1500:]


def test_the_re_exports_are_the_SAME_objects_as_the_originals():
    """Not copies, not wrappers. A test that monkeypatches
    `signal_indicators.compute_adx` must affect callers going through
    `signal_generator.compute_adx`, or the two drift apart silently."""
    proc = _run(
        "from backend.src.services.test_signal import signal_generator as g, "
        "signal_indicators as i\n"
        "for n in ('compute_adx', 'detect_regime', 'calculate_risk_levels'):\n"
        "    assert getattr(g, n) is getattr(i, n), n\n"
    )

    assert proc.returncode == 0, proc.stderr[-1500:]


def test_an_unknown_attribute_still_raises_AttributeError():
    """Negative control. A module-level `__getattr__` that returned something
    for every name would hide typos and make `hasattr` always true."""
    proc = _run(
        "from backend.src.services.test_signal import signal_generator as g\n"
        "try:\n"
        "    g.no_such_name_at_all\n"
        "except AttributeError:\n"
        "    raise SystemExit(0)\n"
        "raise SystemExit('no AttributeError raised')\n"
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr[-500:]
