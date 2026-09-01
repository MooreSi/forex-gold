"""Pure display-formatting helpers behind the Trade Analysis views:
strategy_display_label (Strategy column) and format_duration (Held / Pending
For columns). No DB, no NiceGUI rendering -- these never touch either."""
from backend.src.controllers import history_controller as history


def test_strategy_display_label_known_strategy():
    assert history.strategy_display_label("scale_out") == "Scale Out"


def test_strategy_display_label_short_override():
    assert history.strategy_display_label("conservative") == "Conservative"


def test_strategy_display_label_ea_template():
    """EA Templates ("template:<name>") are user-defined, not one of the
    fixed built-in strategies, so they were never in STRATEGY_NAMES and
    fell through to the "—" placeholder instead of a readable name --
    confirmed live 2026-07-23."""
    assert history.strategy_display_label("template:StealthTest") == "Template: StealthTest"


def test_strategy_display_label_blank_is_placeholder():
    assert history.strategy_display_label("") == "—"


def test_strategy_display_label_unknown_falls_back_to_placeholder():
    assert history.strategy_display_label("some_future_strategy") == "—"


def test_format_duration_none_is_placeholder():
    assert history.format_duration(None) == "—"


def test_format_duration_negative_is_placeholder():
    assert history.format_duration(-5) == "—"


def test_format_duration_seconds():
    assert history.format_duration(45) == "45s"


def test_format_duration_minutes():
    assert history.format_duration(125) == "2m 5s"


def test_format_duration_minutes_exact():
    assert history.format_duration(120) == "2m"


def test_format_duration_hours():
    assert history.format_duration(2 * 3600 + 15 * 60) == "2h 15m"


def test_format_duration_days():
    assert history.format_duration(3 * 86400 + 4 * 3600) == "3d 4h"


def test_format_duration_days_exact():
    assert history.format_duration(2 * 86400) == "2d"


# ── parse_reason ──────────────────────────────────────────────────────────────
# Untested before this move. These pin the MT5 comment vocabulary the column
# depends on -- the strings come from the broker, so a silent change here would
# relabel every closed trade.

def test_parse_reason_stop_loss_bracket_form():
    assert history.parse_reason("[sl 4482.00]") == "SL"


def test_parse_reason_take_profit_bracket_form():
    assert history.parse_reason("[tp 4460.00]") == "TP"


def test_parse_reason_stop_out_is_not_a_manual_close():
    """A margin call must never read as "Manual" -- it is the opposite of one."""
    assert history.parse_reason("so: 1:100") == "Stop-out"


def test_parse_reason_empty_falls_back_to_pnl_sign():
    assert history.parse_reason("", pnl=12.0) == "Manual"
    assert history.parse_reason("unrecognised", pnl=12.0) == "unrecognised"


def test_parse_reason_is_case_insensitive():
    assert history.parse_reason("[SL 4482.00]") == "SL"


# ── broker timestamp handling ─────────────────────────────────────────────────
# The subtle one. MT5 stores UTC+3 wall-clock as if it were a Unix epoch, so
# format_broker_ts reads it as UTC on purpose (that yields broker time), while
# broker_ts_to_local_date must remove the offset first or the monthly calendar
# files trades under the wrong day.

def test_format_broker_ts_reads_as_utc_to_give_broker_time():
    # Epoch that reads as 2026-07-21 14:00 when interpreted as UTC, i.e. broker time.
    assert history.format_broker_ts(1784642400) == "07-21 14:00"


def test_broker_ts_to_local_date_removes_the_three_hour_offset():
    """A trade stamped 01:00 broker time is still the previous local day.

    The offset is passed rather than resolved: +60 is British Summer Time,
    which is what the hardcoded Europe/London gave here before the day moved
    onto the trading clock, so this stays the same assertion it always was --
    and stops depending on where the machine running it happens to be.
    """
    # 2026-07-22 01:00 "broker" -> 2026-07-21 22:00 real UTC -> 23:00 UK (BST).
    from datetime import date
    assert history.broker_ts_to_local_date(1784682000, 60) == date(2026, 7, 21)


def test_broker_ts_helpers_return_placeholders_on_junk():
    assert history.format_broker_ts(None) == "—"
    assert history.broker_ts_to_local_date("not-a-timestamp") is None
    assert history.to_date("not-a-timestamp") is None
