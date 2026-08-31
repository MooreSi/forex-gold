"""The machine fingerprint: the thing a licence is actually bound to.

Two failure directions matter, and they pull against each other:

  * **Too unstable** and a licence stops working after an OS update, on a
    machine nobody touched. That is not hypothetical here -- `guard.enforce()`
    carries a whole fingerprint-drift recovery path because it happened.
  * **Too weak or too shared** and one licence works on machines it was never
    issued for.

Nothing here runs `system_profiler`, `wmic` or `Get-CimInstance` for real; the
collectors are driven with recorded shapes so the derivation is tested rather
than this particular machine.

Includes the wildcard question -- see TestTheWildcardIsNotWired.
"""
from __future__ import annotations

import hashlib
import re

import pytest

from backend.src.config.licence import fingerprint as fp

_FORMAT = re.compile(r"^FOREX-[0-9A-F]{8}-[0-9A-F]{8}-[0-9A-F]{8}-[0-9A-F]{8}$")


@pytest.fixture
def stable(monkeypatch):
    """Drive the platform collectors from a recorded value."""
    box = {"value": "PLATFORM-UUID|HW-UUID|SERIAL123|BOARD-ID"}
    for name in ("_macos_stable_input", "_windows_stable_input",
                 "_fallback_stable_input"):
        monkeypatch.setattr(fp, name, lambda: box["value"])
    return box


class TestTheFormat:
    def test_it_matches_the_shape_keygen_expects(self, stable):
        """KeyGen signs against this exact string. A format change on either
        side silently invalidates every licence ever issued."""
        assert _FORMAT.match(fp.get_fingerprint())

    def test_it_is_derived_from_the_sha256_of_the_stable_input(self, stable):
        h = hashlib.sha256(stable["value"].encode()).hexdigest().upper()

        assert fp.get_fingerprint() == f"FOREX-{h[0:8]}-{h[8:16]}-{h[16:24]}-{h[24:32]}"

    def test_get_sha256_returns_the_whole_hash(self, stable):
        full = fp.get_sha256()

        assert len(full) == 64
        assert full == full.upper()
        assert fp.get_fingerprint().replace("FOREX-", "").replace("-", "") == full[:32]


class TestStability:
    def test_the_same_machine_gives_the_same_answer(self, stable):
        assert fp.get_fingerprint() == fp.get_fingerprint()

    def test_a_different_machine_gives_a_different_answer(self, stable):
        first = fp.get_fingerprint()
        stable["value"] = "OTHER-UUID|OTHER-HW|SERIAL999|OTHER-BOARD"

        assert fp.get_fingerprint() != first

    def test_a_single_changed_field_changes_it(self, stable):
        """Control for the test above: it must not be dominated by one field
        while quietly ignoring the rest."""
        first = fp.get_fingerprint()
        stable["value"] = "PLATFORM-UUID|HW-UUID|SERIAL123|DIFFERENT-BOARD"

        assert fp.get_fingerprint() != first


class TestTheFallbackNeverCrashesStartup:
    """`get_fingerprint` runs before anything else at boot. If it raises, the
    app cannot even reach the activation screen -- so it has a last-resort
    path, and that path must still produce a usable id."""

    def test_a_collector_that_explodes_still_yields_a_fingerprint(self,
                                                                  monkeypatch):
        def _boom():
            raise OSError("system_profiler is not available")
        monkeypatch.setattr(fp, "_compute_hash", _boom)

        result = fp.get_fingerprint()

        assert result.startswith("FOREX-")
        assert len(result) > 10

    def test_the_same_is_true_of_get_sha256(self, monkeypatch):
        def _boom():
            raise OSError("nope")
        monkeypatch.setattr(fp, "_compute_hash", _boom)

        assert len(fp.get_sha256()) == 64

    def test_the_fallback_is_NOT_a_constant(self, monkeypatch):
        """A fallback that returned the same value on every machine would hand
        every install the same identity -- one licence for all of them."""
        def _boom():
            raise OSError("nope")
        monkeypatch.setattr(fp, "_compute_hash", _boom)

        import uuid
        monkeypatch.setattr(uuid, "getnode", lambda: 0xAA_BB_CC_DD_EE_FF)
        first = fp.get_fingerprint()
        monkeypatch.setattr(uuid, "getnode", lambda: 0x11_22_33_44_55_66)

        assert fp.get_fingerprint() != first


class TestFieldExtraction:
    def test_extract_finds_the_field(self):
        text = "      Serial Number (system): C02XY1234567\n"

        assert fp._extract(r"Serial Number \(system\):\s*(\S+)", text) == "C02XY1234567"

    def test_extract_returns_the_default_when_absent(self):
        assert fp._extract(r"Nothing:\s*(\S+)", "unrelated output", "MISSING") == "MISSING"

    def test_a_collector_timing_out_returns_empty_rather_than_raising(self,
                                                                      monkeypatch):
        """`_run` shells out with a timeout. On Windows 24H2 the wmic fallback
        chain depends on this returning quietly."""
        import subprocess

        def _timeout(*_a, **_kw):
            raise subprocess.TimeoutExpired(cmd="x", timeout=1)
        monkeypatch.setattr(subprocess, "run", _timeout)

        assert fp._run("anything") == ""


class TestTheWildcardIsNotWired:
    """`TEST_WILDCARD` and `is_test_wildcard()` exist, and the module docstring
    says the wildcard "bypasses all hardware checks".

    **Nothing in this repository consults either of them.** Checked across the
    whole tree on 2026-08-31: the only occurrences are the definition and the
    docstring. So there is no bypass in this codebase -- but there is a
    ready-made hook for one, described in a docstring as supported, which is a
    trap for whoever reads it next and decides to "restore" the behaviour.

    This test makes wiring it up a deliberate act that has to go red first.
    Raised for the owner as docs/simon-handover/014.
    """

    def test_nothing_in_the_app_consults_the_wildcard(self):
        import pathlib

        repo = pathlib.Path(__file__).resolve().parents[2]
        hits = []
        for root in ("backend", "frontend", "tools", "run.py"):
            base = repo / root
            paths = [base] if base.is_file() else [
                p for p in base.rglob("*.py") if "__pycache__" not in p.parts
            ]
            for p in paths:
                text = p.read_text(encoding="utf-8", errors="replace")
                if "TEST_WILDCARD" in text or "is_test_wildcard" in text:
                    rel = str(p.relative_to(repo)).replace("\\", "/")
                    if rel != "backend/src/config/licence/fingerprint.py":
                        hits.append(rel)

        assert hits == [], (
            f"the test wildcard is now consulted by: {hits}. A licence bound "
            f"to TEST_WILDCARD would work on every machine. If this is "
            f"deliberate, it needs the owner's sign-off -- it is a licence "
            f"bypass, which this project forbids adding."
        )

    def test_a_real_fingerprint_is_never_the_wildcard(self, stable):
        assert fp.is_test_wildcard(fp.get_fingerprint()) is False

    def test_the_predicate_still_works_as_written(self):
        """Not an endorsement -- just so that if it IS wired up one day, its
        behaviour is known rather than assumed."""
        assert fp.is_test_wildcard(fp.TEST_WILDCARD) is True
        assert fp.is_test_wildcard("FOREX-AAAAAAAA-BBBBBBBB-CCCCCCCC-DDDDDDDD") is False
