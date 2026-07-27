"""Pure display-formatting helpers on the Trade Analysis (history.py) page:
_strategy_display_label (Strategy column) and _fmt_duration (Held / Pending
For columns). No DB, no NiceGUI rendering -- these never touch either."""
from frontend.pages import history


def test_strategy_display_label_known_strategy():
    assert history._strategy_display_label("scale_out") == "Scale Out"


def test_strategy_display_label_short_override():
    assert history._strategy_display_label("conservative") == "Conservative"


def test_strategy_display_label_ea_template():
    """EA Templates ("template:<name>") are user-defined, not one of the
    fixed built-in strategies, so they were never in STRATEGY_NAMES and
    fell through to the "—" placeholder instead of a readable name --
    confirmed live 2026-07-23."""
    assert history._strategy_display_label("template:StealthTest") == "Template: StealthTest"


def test_strategy_display_label_blank_is_placeholder():
    assert history._strategy_display_label("") == "—"


def test_strategy_display_label_unknown_falls_back_to_placeholder():
    assert history._strategy_display_label("some_future_strategy") == "—"


def test_fmt_duration_none_is_placeholder():
    assert history._fmt_duration(None) == "—"


def test_fmt_duration_negative_is_placeholder():
    assert history._fmt_duration(-5) == "—"


def test_fmt_duration_seconds():
    assert history._fmt_duration(45) == "45s"


def test_fmt_duration_minutes():
    assert history._fmt_duration(125) == "2m 5s"


def test_fmt_duration_minutes_exact():
    assert history._fmt_duration(120) == "2m"


def test_fmt_duration_hours():
    assert history._fmt_duration(2 * 3600 + 15 * 60) == "2h 15m"


def test_fmt_duration_days():
    assert history._fmt_duration(3 * 86400 + 4 * 3600) == "3d 4h"


def test_fmt_duration_days_exact():
    assert history._fmt_duration(2 * 86400) == "2d"
