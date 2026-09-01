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


class TestThereIsNoMasterFingerprint:
    """`TEST_WILDCARD` and `is_test_wildcard()` were removed on 2026-09-01, on
    the owner's instruction: *"there should be no master key, the keygen is kept
    in a separate folder purposely"*.

    Nothing had ever consulted them, so no bypass existed — but the module
    docstring described the wildcard as bypassing all hardware checks, which is
    an invitation to restore behaviour that was never there. See
    docs/simon-handover/014.

    This test replaces the earlier "not wired" one. It is stronger: rather than
    checking nothing *uses* the hook, it checks the hook does not exist, and
    that no equivalent has been added under another name.
    """

    def test_the_wildcard_constant_is_gone(self):
        assert not hasattr(fp, "TEST_WILDCARD")

    def test_the_predicate_is_gone(self):
        assert not hasattr(fp, "is_test_wildcard")

    def test_nothing_in_the_app_defines_a_replacement(self):
        """A different name for the same idea is the same problem. Anything
        that looks like a fixed, shared machine id would work on every
        install."""
        import pathlib as _pl

        repo = _pl.Path(__file__).resolve().parents[2]
        offenders = []
        for root in ("backend", "frontend", "tools", "run.py"):
            base = repo / root
            paths = [base] if base.is_file() else [
                q for q in base.rglob("*.py") if "__pycache__" not in q.parts
            ]
            for q in paths:
                text = q.read_text(encoding="utf-8", errors="replace")
                for marker in ("TEST_WILDCARD", "is_test_wildcard",
                               "MASTER_FINGERPRINT", "WILDCARD_MACHINE"):
                    if marker in text and "fingerprint.py" not in str(q):
                        offenders.append(f"{q.relative_to(repo)}: {marker}")

        assert offenders == [], (
            f"a master-fingerprint hook has reappeared: {offenders}. A licence "
            f"bound to a fixed id would work on every machine, which this "
            f"project forbids."
        )

    def test_a_real_fingerprint_is_still_machine_specific(self, stable):
        """The positive side of the same property."""
        first = fp.get_fingerprint()
        stable["value"] = "SOMEONE-ELSES-MACHINE|X|Y|Z"

        assert fp.get_fingerprint() != first
