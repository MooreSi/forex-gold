"""The frontend reads and writes app config through a controller.

`backend.src.config` was the single largest remaining coupling in
`frontend-reaches-the-backend-through-controllers`: eight of the fifteen
frontend source units imported it directly, which is more than the whole gap
between the count and its baseline.

CLAUDE.md flags this module by name -- *"backend.src.config imports from
frontend COUNT against the controller-boundary contract"* -- and the frontend
only ever needed four things from it: read the whole config, read one key,
write some keys, and ask whether debug mode is on. Those are now
`settings_controller` functions.

The pairing matters as much as the boundary. `save_config` writing somewhere
`load_config` does not read would lose a user's settings silently, so that
round trip is asserted rather than assumed.
"""
from __future__ import annotations

import ast
import pathlib

import backend.src.config as cfg_module
from backend.src.controllers import settings_controller as ctl

REPO = pathlib.Path(__file__).resolve().parents[2]


def test_the_controller_can_read_the_whole_config():
    assert isinstance(ctl.load_config(), dict)


def test_the_controller_reads_a_single_key_with_a_default():
    """The three live call shapes are all (key, default)."""
    assert ctl.get_config("account_env", "demo") in ("demo", "live")
    assert ctl.get_config("__definitely_not_a_key__", "fallback") == "fallback"


def test_reading_one_key_agrees_with_reading_the_whole_config():
    """Two readers that disagree would make a settings page show one value and
    save another."""
    whole = ctl.load_config()
    for key in ("account_env", "starting_balance"):
        if key in whole:
            assert ctl.get_config(key, None) == whole[key]


def test_the_debug_flag_comes_through_the_same_door():
    assert ctl.is_debug() == cfg_module.is_debug()


def test_saving_and_loading_are_the_same_store(tmp_path, monkeypatch):
    """The round trip. A save that lands somewhere load does not read loses a
    user's settings with no error anywhere."""
    written = {}
    monkeypatch.setattr(cfg_module, "save_to_yaml", lambda values: written.update(values))
    monkeypatch.setattr(cfg_module, "load", lambda: dict(written))

    ctl.save_config({"account_env": "demo", "starting_balance": 2500.0})

    assert ctl.load_config()["starting_balance"] == 2500.0
    assert written == {"account_env": "demo", "starting_balance": 2500.0}


def test_the_controller_declares_these_public():
    for name in ("load_config", "save_config", "get_config", "is_debug"):
        assert name in ctl.__all__, f"{name} is missing from settings_controller.__all__"


def test_no_frontend_module_imports_the_app_config_directly():
    """The boundary itself, including the function-local imports -- most of
    these were inside functions, which is exactly where they hide.

    Scoped to `backend.src.config` itself. Its subpackages are different
    things with their own reasons: `config.licence` and
    `config.licence.fingerprint` are the licence store and the machine
    fingerprint, not app settings, and they are still their own coupling
    edges. Widening this test to cover them would assert work that has not
    been done.
    """
    offenders = []
    for path in (REPO / "frontend").rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            mod = None
            if isinstance(node, ast.Import):
                mod = next((a.name for a in node.names
                            if a.name == "backend.src.config"), None)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module if node.module == "backend.src.config" else None
            if mod:
                offenders.append(f"{path.relative_to(REPO)}:{node.lineno}")
    assert offenders == [], (
        "these reach past the controller layer for app config:\n  "
        + "\n  ".join(offenders)
    )


def test_the_contract_drops_below_its_baseline():
    from tools.refactor_audit import import_contracts as ic

    count = ic.check().counts["frontend-reaches-the-backend-through-controllers"]
    assert count <= 50, (
        f"{count} edges against a baseline of 50 -- removing the eight "
        "backend.src.config units should take 56 to 48"
    )
