"""TP HIT parsing is gone.

Owner, 2026-09-02: "remove the 'Enable TP HIT Parsing' - not needed".

Safe to remove outright rather than just hide the toggle: the handler only
logged the message and sent a Telegram notice. Its own docstring recorded the
constraint — "never moves SL or closes anything by itself", confirmed with the
owner 2026-07-22 — so nothing on the money path changes, and the only
behaviour lost is a notification.

Removing the toggle while leaving the parsing on would have kept sending those
notices with no way to stop them, which is the opposite of what was asked.
"""
from __future__ import annotations

import pathlib

REPO = pathlib.Path(__file__).resolve().parents[2]


def _code(rel: str) -> str:
    """Source with comments stripped, so a substring search cannot pass by
    matching prose that explains the removal."""
    text = (REPO / rel).read_text(encoding="utf-8")
    return "\n".join(ln for ln in text.splitlines()
                     if not ln.strip().startswith("#"))


class TestTheSettingIsGone:
    def test_the_toggle_is_not_offered(self):
        assert "lk_enable_tp_hit_parsing" not in _code(
            "frontend/pages/telegram/_keywords.py")

    def test_the_label_is_not_rendered(self):
        assert "TP HIT Parsing" not in _code(
            "frontend/pages/telegram/_keywords.py")


class TestTheParsingIsGone:
    def test_the_handler_no_longer_exists(self):
        assert "try_handle_tp_hit_trigger" not in _code(
            "backend/src/services/telegram/keyword_triggers.py")

    def test_the_scanner_no_longer_calls_it(self):
        assert "try_handle_tp_hit_trigger" not in _code(
            "backend/src/services/signals/scan_messages.py")

    def test_the_setting_is_read_nowhere(self):
        """A settings key nobody reads and nobody can set is the kind of
        orphan that confuses the next person to look."""
        for rel in ("backend/src/services/telegram/keyword_triggers.py",
                    "backend/src/services/signals/scan_messages.py"):
            assert "lk_enable_tp_hit_parsing" not in _code(rel), rel


class TestTheOtherTriggersSurvive:
    """CLOSE ALL and RISK FREE / BE do act on trades. They stay."""

    def test_close_all_still_exists(self):
        assert "try_handle_close_all_trigger" in _code(
            "backend/src/services/telegram/keyword_triggers.py")

    def test_risk_free_be_still_exists(self):
        assert "try_handle_risk_free_be_trigger" in _code(
            "backend/src/services/telegram/keyword_triggers.py")

    def test_the_scanner_still_calls_both(self):
        code = _code("backend/src/services/signals/scan_messages.py")
        assert "try_handle_close_all_trigger" in code
        assert "try_handle_risk_free_be_trigger" in code
