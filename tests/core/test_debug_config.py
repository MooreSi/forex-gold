"""Debug-mode flag: default off, env-over-yaml, and hard DB isolation.

local-debug-mode pack, task 010. The flag lets the whole app boot on fakes with
zero credentials; the DB isolation line is the one that matters most — a debug
boot must never open a demo/live database file.
"""
from __future__ import annotations

import pytest

import backend.src.config as cfg_module


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    monkeypatch.delenv("FOREX_DEBUG_MODE", raising=False)
    monkeypatch.delenv("ACCOUNT_ENV", raising=False)


def test_debug_mode_defaults_false(monkeypatch):
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})
    cfg = cfg_module.load()
    assert cfg["debug_mode"] is False
    assert cfg_module.is_debug() is False


def test_debug_mode_env_overrides_yaml(monkeypatch):
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {"debug_mode": False})
    monkeypatch.setenv("FOREX_DEBUG_MODE", "1")
    cfg = cfg_module.load()
    assert cfg["debug_mode"] is True
    assert cfg_module.is_debug() is True


def test_debug_mode_yaml_alone_enables(monkeypatch):
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {"debug_mode": True})
    assert cfg_module.load()["debug_mode"] is True


def test_debug_db_path_is_isolated(monkeypatch):
    """The killer test: a debug boot can never open a demo/live DB file."""
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {"debug_mode": True, "account_env": "live"})
    cfg = cfg_module.load()
    assert cfg["db_path"].endswith("forex_trader_debug.db")
    assert "live" not in cfg["db_path"].rsplit("\\", 1)[-1]


def test_db_path_unchanged_when_debug_off(monkeypatch):
    """Regression: demo/live naming byte-identical without the flag."""
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})
    assert cfg_module.load()["db_path"].endswith("forex_trader_demo.db")
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {"account_env": "live"})
    assert cfg_module.load()["db_path"].endswith("forex_trader_live.db")


def test_is_debug_helper_reflects_config(monkeypatch):
    """Negative control: the helper flips when the config flips."""
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {"debug_mode": True})
    cfg_module.load()
    assert cfg_module.is_debug() is True
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})
    cfg_module.load()
    assert cfg_module.is_debug() is False


def test_load_output_identical_apart_from_new_key(monkeypatch):
    """A config with no new key behaves exactly as today."""
    monkeypatch.setattr(cfg_module, "_load_yaml", lambda: {})
    cfg = cfg_module.load()
    cfg.pop("debug_mode")
    assert "debug" not in cfg["db_path"]  # nothing else about the dict changed shape