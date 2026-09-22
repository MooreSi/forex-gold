"""The open-source build boots with no licence key and no password.

Owner, 2026-09-22: the repo is being opened for the community, so neither gate
may stand between a fresh clone and a running dashboard. **Nothing is deleted**
-- `config/licence/` is untouched and every test in this directory still runs
against it -- the two call sites in `run.py` are conditional instead.

What this file pins is the WIRING, because the wiring is the whole change and
a silent revert of it is the failure mode: a `run.py` that calls
`_licence_enforce()` unconditionally again would lock every clone out, and one
that installs the auth gate again would put a password prompt in front of a
dashboard that has no password to give.

The ordering rules those two call sites still obey are in
`test_activation_screen_has_a_database.py`, which reads the same source and did
not change: the database still opens before the licence step, and the licence
step still comes before anything that trades.
"""
from __future__ import annotations

import pathlib
import re

RUN_PY = pathlib.Path("run.py").read_text(encoding="utf-8")


def _line_containing(needle: str) -> str:
    m = [ln for ln in RUN_PY.splitlines() if needle in ln and not ln.lstrip().startswith("#")]
    assert m, f"{needle!r} not found in run.py outside a comment"
    return m[0]


class TestTheLicenceCheck:
    def test_the_call_is_still_there(self):
        """Disabled, not removed. Deleting it would make re-enabling a rewrite
        rather than a one-line constant change."""
        assert re.search(r"^\s+_licence_enforce\(\)", RUN_PY, re.M)

    def test_it_only_runs_when_the_build_requires_a_licence(self):
        """The guard immediately above the call, not somewhere else in the
        file: `enforce()` never returns once it shows the activation screen."""
        m = re.search(
            r"if\s+(?:_?edition\.)?licence_required\(\):\s*\n\s+_licence_enforce\(\)",
            RUN_PY,
        )
        assert m, "run.py must call _licence_enforce() only under licence_required()"


class TestTheLoginGate:
    def test_the_gate_is_installed_only_when_the_build_requires_one(self):
        line = _line_containing("install_auth_gate=")
        assert "authentication_required()" in line, line

    def test_it_is_not_hardcoded_off(self):
        """`install_auth_gate=False` would be the bypass with no way back."""
        assert "install_auth_gate=False" not in RUN_PY
