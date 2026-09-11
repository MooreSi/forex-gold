"""`measure_repo` must be importable before `reversal_engine_repo`.

The repo re-exports this module's writers from its own footer, so a
module-level `from reversal_engine_repo import get_db` in `measure_repo`
closes a cycle. It went unnoticed because every caller happened to import the
repo first; `excursion_backfill` does not, and the failure is an ImportError
at collection time rather than anything subtle.

A subprocess, because an in-process import is cached and would prove nothing
about ordering after the first test to touch either module.
"""
from __future__ import annotations

import subprocess
import sys


def test_importing_measure_repo_first_does_not_close_a_cycle():
    r = subprocess.run(
        [sys.executable, "-c",
         "from backend.src.services.reversal_engine import measure_repo; "
         "from backend.src.services.reversal_engine import reversal_engine_repo as r; "
         "assert r.record_excursion is measure_repo.record_excursion; "
         "print('ok')"],
        capture_output=True, text=True,
    )
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout
