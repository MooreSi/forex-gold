"""facade_audit is delegation_checker's successor for the M4 dissolution.

delegation_checker went vacuous when forex_trader/core was dissolved (its
CORE_DIR glob returns nothing, so it enforces nothing). What M4 actually
needs guarded is the new premise: the runtime class only ever SHRINKS, and
its public surface is exactly the curated allowlist. These tests pin that
tool before any wrapper is deleted, so every deletion is measured by a live
ratchet rather than a dead one.
"""
from __future__ import annotations

import json

import pytest

from tools.refactor_audit import facade_audit as fa


def census_of(src: str) -> dict:
    return fa.census(src)


_SYNTHETIC = '''
class TradingRuntime:
    def __init__(self):
        self.x = 1

    def public_allowed(self):
        return impl(self.x)

    def _private_helper(self):
        a = self.x + 1
        return a
'''

_SYNTHETIC_EXTRA_PUBLIC = _SYNTHETIC + '''
    def public_rogue(self):
        return impl2(self.x)
'''


def test_census_counts_methods_and_classifies_wrappers():
    c = census_of(_SYNTHETIC)
    assert set(c) == {"__init__", "public_allowed", "_private_helper"}
    assert c["public_allowed"]["public"] is True
    assert c["public_allowed"]["wrapper"] is True
    assert c["_private_helper"]["public"] is False
    assert c["_private_helper"]["wrapper"] is False


def test_method_count_may_only_shrink():
    baseline = {"method_count": 2}
    allowlist = {"public_allowed", "public_rogue"}
    failures = fa.check(census_of(_SYNTHETIC_EXTRA_PUBLIC), baseline, allowlist)
    assert any("method count" in f for f in failures)


def test_public_method_outside_allowlist_fails():
    baseline = {"method_count": 99}
    failures = fa.check(census_of(_SYNTHETIC_EXTRA_PUBLIC), baseline,
                        allowlist={"public_allowed"})
    assert any("public_rogue" in f for f in failures)


def test_shrunken_source_passes():
    baseline = {"method_count": 99}
    assert fa.check(census_of(_SYNTHETIC), baseline,
                    allowlist={"public_allowed"}) == []


def test_the_real_runtime_is_green_against_the_checked_in_baselines():
    """The repo state must always pass its own ratchet -- same contract as
    structure_gates. Survivor methods that will outlive the dissolution
    must be present; the exact count lives in the baseline, not here."""
    c = fa.census(fa.RUNTIME_PATH.read_text(encoding="utf-8"))
    for survivor in ("startup", "shutdown", "close_trade", "get_tick",
                     "_make_close_trade_ctx"):
        assert survivor in c
    failures = fa.check(c, fa.load_baseline(), fa.load_allowlist())
    assert failures == [], failures


def test_update_baseline_tightens_to_current(tmp_path, monkeypatch):
    monkeypatch.setattr(fa, "BASELINE_PATH", tmp_path / "b.json")
    fa.update_baseline(census_of(_SYNTHETIC))
    written = json.loads((tmp_path / "b.json").read_text(encoding="utf-8"))
    assert written["method_count"] == 3
