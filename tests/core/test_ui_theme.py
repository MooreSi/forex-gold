"""core_ui_theme.py: Settings > Theme -- dark-mode color presets persisted
via app_config. Added 2026-07-24. Only the neutral/structural gray classes
are overridden per preset; semantic status colors (green/red/yellow/etc.)
are deliberately untouched -- see the module's own docstring for why.
"""
import os
import tempfile

import pytest

from backend.src.db import database as db
from backend.src.utils import theme as theme_mod


def _reset_thread_local_connection():
    conn = getattr(db._thread_local, "conn", None)
    if conn is not None:
        conn.close()
        del db._thread_local.conn
    if hasattr(db._thread_local, "depth"):
        del db._thread_local.depth


def _reset_db_worker_thread_connection():
    db._db_executor.submit(_reset_thread_local_connection).result()


@pytest.fixture
def fresh_db():
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init(path)
    yield db
    _reset_thread_local_connection()
    _reset_db_worker_thread_connection()
    os.remove(path)


def test_get_theme_defaults_to_midnight_with_no_override(fresh_db):
    assert theme_mod.get_theme() == "midnight"


def test_set_theme_persists_and_is_read_back(fresh_db):
    theme_mod.set_theme("slate")
    assert theme_mod.get_theme() == "slate"


def test_set_theme_unknown_raises(fresh_db):
    with pytest.raises(ValueError):
        theme_mod.set_theme("not_a_real_theme")


def test_get_theme_falls_back_to_default_on_corrupt_stored_value(fresh_db):
    db.set_app_config("ui_theme", "not_a_real_theme")
    assert theme_mod.get_theme() == "midnight"


def test_every_theme_has_a_label_description_and_swatch():
    for name in theme_mod.THEMES:
        assert name in theme_mod.THEME_LABELS
        assert name in theme_mod.THEME_DESCRIPTIONS
        assert name in theme_mod.THEME_SWATCHES
        assert len(theme_mod.THEME_SWATCHES[name]) == 3


def test_midnight_has_no_override_css():
    # midnight is the literal default already hard-coded across every page
    # -- it must not appear in the generated override stylesheet.
    assert 'data-fx-theme="midnight"' not in theme_mod.THEME_HEAD_CSS


def test_non_default_themes_appear_in_override_css():
    for name in ("slate", "ember", "contrast"):
        assert f'data-fx-theme="{name}"' in theme_mod.THEME_HEAD_CSS


def test_override_css_never_touches_semantic_status_colors():
    # Only neutral gray-scale classes may be remapped -- green/red/yellow/
    # blue/purple/teal/orange/amber carry meaning (profit/loss/warning) and
    # must be identical across every theme.
    for cls in ("green", "red", "yellow", "blue", "purple", "teal", "orange", "amber"):
        assert f"-{cls}-" not in theme_mod.THEME_HEAD_CSS
