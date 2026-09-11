"""The built-in EA template is created through the controller.

The templates page may not import the service directly (the frontend layer
rule), so `install_builtin_template` and `BUILTIN_PRESET_NAME` are the only
route the Add Built-in button has.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.src.controllers import broker_controller as bc


def test_the_preset_name_is_re_exported_for_the_page():
    assert isinstance(bc.BUILTIN_PRESET_NAME, str)
    assert bc.BUILTIN_PRESET_NAME.strip()


def test_install_forwards_to_the_preset_service():
    with patch.object(bc._presets, "install", return_value={"name": "x"}) as fwd:
        assert bc.install_builtin_template() == {"name": "x"}
    assert fwd.call_count == 1


def test_install_passes_an_explicit_overwrite_through():
    with patch.object(bc._presets, "install", return_value={}) as fwd:
        bc.install_builtin_template(overwrite=True)
    assert fwd.call_args.kwargs["overwrite"] is True
