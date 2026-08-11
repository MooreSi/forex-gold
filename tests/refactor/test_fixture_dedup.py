"""Fixture-duplication ratchet (stage2 phase3/040).

`fresh_db` was once defined locally in 114 test files (17 variants) and
`_FakeBridge` in 69. Every DB-internal change then breaks dozens of files
at once, and ad-hoc fakes drift from the real bridge surface. The
canonical `fresh_db` lives in tests/conftest.py; 35 byte-equivalent copies
were migrated onto it (2026-08-11). The remaining locals are genuine
variants — they may only shrink, never grow, until each is read, migrated
and deleted. When phase-5's shared FakeMT5Bridge lands, new tests use it
instead of another local `_FakeBridge`.

Also pins the MT5-safety invariant: no test module imports MetaTrader5.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests"

# Shrinking baselines — lower them as migrations land; never raise them.
FRESH_DB_LOCAL_DEFS_MAX = 66
FAKE_BRIDGE_CLASSES_MAX = 56


def _count(pattern: str, glob: str) -> int:
    rx = re.compile(pattern, re.MULTILINE)
    return sum(
        len(rx.findall(p.read_text(encoding="utf-8"))) for p in TESTS.rglob(glob)
    )


def test_fresh_db_locals_only_shrink():
    """New tests take fresh_db from tests/conftest.py — another local copy
    is one more file that breaks when the DB layer moves."""
    count = _count(r"^\s*def fresh_db\b", "test_*.py")
    assert count <= FRESH_DB_LOCAL_DEFS_MAX, (
        f"{count} local fresh_db definitions (baseline {FRESH_DB_LOCAL_DEFS_MAX}) — "
        "use the conftest fixture instead of defining another copy"
    )


def test_fake_bridge_classes_only_shrink():
    count = _count(r"^\s*class _FakeBridge\b", "*.py")
    assert count <= FAKE_BRIDGE_CLASSES_MAX, (
        f"{count} local _FakeBridge classes (baseline {FAKE_BRIDGE_CLASSES_MAX}) — "
        "share a fake instead of writing another"
    )


def test_baselines_are_not_slack():
    """The baselines must sit AT the real counts, not above them — slack in
    a shrinking baseline is room to regress invisibly."""
    assert _count(r"^\s*def fresh_db\b", "test_*.py") == FRESH_DB_LOCAL_DEFS_MAX
    assert _count(r"^\s*class _FakeBridge\b", "*.py") == FAKE_BRIDGE_CLASSES_MAX


def test_the_counters_can_count(tmp_path):
    """Negative control: the scanners are not blind."""
    rx_fresh = re.compile(r"^\s*def fresh_db\b", re.M)
    rx_fake = re.compile(r"^\s*class _FakeBridge\b", re.M)
    sample = "def fresh_db():\n    pass\n\nclass _FakeBridge:\n    pass\n"
    assert rx_fresh.search(sample) and rx_fake.search(sample)


def test_no_test_imports_metatrader5():
    """MT5-safety: no test may import the real MetaTrader5 module — a test
    that can reach a terminal can reach an order."""
    offenders = [
        str(p.relative_to(TESTS))
        for p in TESTS.rglob("*.py")
        if re.search(r"^\s*(import MetaTrader5|from MetaTrader5)", p.read_text(encoding="utf-8"), re.M)
    ]
    assert offenders == []
