"""Gates must fail closed when the tree they audit has moved.

The structure gate and the import-contract gate each scan fixed core
directories (controllers, services, frontend, ...). Previously, if such a
directory was missing they silently skipped it and reported clean -- the same
scan-nothing-and-pass failure that let the orphan detector rot. If a core layer
is renamed or moved, the gate must ERROR, not pass.
"""
from __future__ import annotations

import pytest

from tools.refactor_audit import import_contracts as ic
from tools.refactor_audit import structure_gates as sg


def test_import_contract_fails_closed_on_missing_package(tmp_path, monkeypatch):
    monkeypatch.setattr(ic, "REPO_ROOT", tmp_path)  # empty tree
    contract = ic.CONTRACTS[0]
    assert contract.source_packages  # negative-control: it does scan something
    with pytest.raises(SystemExit):
        ic.violations_for(contract)


def test_structure_gate_fails_closed_on_missing_controller_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(sg.od, "REPO_ROOT", tmp_path)  # controllers/ absent
    with pytest.raises(SystemExit):
        sg.controller_loc_report()


def test_structure_gate_runs_on_the_real_tree():
    """Negative control: with the real tree present, it does NOT raise."""
    sg.controller_loc_report()  # controllers/ exists -> returns normally
