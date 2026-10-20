"""Gold positioning: CFTC Commitments of Traders and GLD holdings (docs/todo/024).

Two weekly/daily reads gold desks watch and this app had neither of. Both are
display-only: nothing decides anything from them.

No network: the fetchers are replaced with canned rows and a workbook built
here, in the shape the two sources returned on 2026-10-20.
"""
from __future__ import annotations

import io
import json
import zipfile

import pytest

from backend.src.services.market import positioning as pos


def _cot_row(date, long_, short, oi=400_000):
    return {"report_date_as_yyyy_mm_dd": f"{date}T00:00:00.000",
            "open_interest_all": str(oi),
            "m_money_positions_long_all": str(long_),
            "m_money_positions_short_all": str(short)}


class TestCotSummary:
    def test_the_latest_week_and_its_change(self):
        rows = [_cot_row("2026-10-13", 124_747, 17_658, 396_345),
                _cot_row("2026-10-06", 127_101, 16_114)]
        got = pos.cot_summary(rows)

        assert got["report_date"] == "2026-10-13"
        assert got["mm_long"] == 124_747
        assert got["mm_short"] == 17_658
        assert got["mm_net"] == 107_089
        assert got["net_change"] == 107_089 - (127_101 - 16_114)
        assert got["net_pct_oi"] == pytest.approx(107_089 / 396_345 * 100, abs=0.05)

    def test_the_change_is_against_the_week_before_not_the_oldest(self):
        rows = [_cot_row("2026-09-29", 0, 0), _cot_row("2026-10-06", 100, 0),
                _cot_row("2026-10-13", 130, 0)]
        assert pos.cot_summary(rows)["net_change"] == 30

    def test_rows_in_any_order(self):
        rows = [_cot_row("2026-10-06", 10, 0), _cot_row("2026-10-13", 30, 0)]
        assert pos.cot_summary(rows)["report_date"] == "2026-10-13"

    def test_the_index_is_where_net_sits_in_its_three_year_range(self):
        rows = [_cot_row(f"2026-{m:02d}-01", net, 0) for m, net in
                ((1, 0), (2, 100), (3, 50))]
        assert pos.cot_summary(rows)["cot_index"] == pytest.approx(50.0)

    def test_at_the_top_of_the_range_the_index_is_100(self):
        rows = [_cot_row("2026-01-01", 0, 0), _cot_row("2026-02-01", 100, 0)]
        assert pos.cot_summary(rows)["cot_index"] == pytest.approx(100.0)

    def test_at_the_bottom_it_is_0(self):
        rows = [_cot_row("2026-01-01", 100, 0), _cot_row("2026-02-01", 0, 0)]
        assert pos.cot_summary(rows)["cot_index"] == pytest.approx(0.0)

    def test_only_the_last_156_weeks_set_the_range(self):
        # A huge net four years ago must not squash today's index.
        old = [_cot_row("2022-01-04", 1_000_000, 0)]
        recent = [_cot_row(f"2026-{1 + i // 4:02d}-{1 + (i % 4) * 7:02d}", 100 + i, 0)
                  for i in range(10)]
        got = pos.cot_summary(old + recent * 1)

        assert got["cot_index"] == pytest.approx(100.0)

    def test_a_flat_range_is_the_middle(self):
        rows = [_cot_row("2026-01-01", 5, 0), _cot_row("2026-02-01", 5, 0)]
        assert pos.cot_summary(rows)["cot_index"] == pytest.approx(50.0)

    def test_a_short_history_is_marked(self):
        rows = [_cot_row("2026-10-13", 5, 0)]
        got = pos.cot_summary(rows)

        assert got["weeks"] == 1
        assert got["net_change"] is None

    def test_no_rows_is_none(self):
        assert pos.cot_summary([]) is None

    def test_unparseable_rows_are_skipped(self):
        rows = [{"report_date_as_yyyy_mm_dd": "garbage"}, _cot_row("2026-10-13", 5, 1)]
        assert pos.cot_summary(rows)["mm_net"] == 4

    def test_the_series_is_oldest_first_and_bounded(self):
        rows = [_cot_row(f"20{20 + i // 52}-01-01", i, 0) for i in range(5)]
        got = pos.cot_summary(rows)

        assert [p["date"] for p in got["series"]] == sorted(p["date"] for p in got["series"])
        assert len(got["series"]) <= pos.SERIES_WEEKS


def _xlsx(rows: list[list]) -> bytes:
    """A workbook in the GLD archive's shape: a disclaimer sheet, then the
    data sheet, dates and headers as shared strings."""
    strings: list[str] = []

    def s(v):
        if v not in strings:
            strings.append(v)
        return strings.index(v)

    def cell(ref, v):
        if isinstance(v, str):
            return f'<c r="{ref}" t="s"><v>{s(v)}</v></c>'
        return f'<c r="{ref}"><v>{v}</v></c>'

    cols = "ABCDEFGHIJK"
    body = "".join(
        f'<row r="{i + 1}">' + "".join(cell(f"{cols[j]}{i + 1}", v)
                                       for j, v in enumerate(r) if v is not None) + "</row>"
        for i, r in enumerate(rows))
    data = f'<worksheet><sheetData>{body}</sheetData></worksheet>'
    disclaimer = f'<worksheet><sheetData><row r="1">{cell("A1", "DISCLAIMER")}</row></sheetData></worksheet>'
    sst = "<sst>" + "".join(f"<si><t>{x}</t></si>" for x in strings) + "</sst>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", disclaimer)
        z.writestr("xl/worksheets/sheet2.xml", data)
        z.writestr("xl/sharedStrings.xml", sst)
    return buf.getvalue()


_HEAD = ["Date", "Closing Price", "Ounces of Gold per Share", "NAV", "Indicative",
         "Mid", "Premium", "Daily Share Volume", "Total Ounces of Gold in the Trust",
         "Tonnes of Gold", "Total Net Asset Value in the Trust"]


def _day(date, tonnes):
    return [date, 380.0, 0.0916, 380.0, 380.0, 380.0, 0.01, 5_000_000, 34_000_000.0,
            tonnes, 1.4e11]


class TestGld:
    def test_it_reads_date_and_tonnes(self):
        blob = _xlsx([_HEAD, _day("15-Oct-2026", 1053.91), _day("16-Oct-2026", 1059.04)])
        assert pos.gld_series(blob) == [("2026-10-15", 1053.91), ("2026-10-16", 1059.04)]

    def test_holidays_and_blank_rows_are_skipped(self):
        blob = _xlsx([_HEAD, _day("15-Oct-2026", 1053.91),
                      ["16-Oct-2026", "US Holiday"], [None],
                      _day("19-Oct-2026", 1058.47)])
        assert [d for d, _ in pos.gld_series(blob)] == ["2026-10-15", "2026-10-19"]

    def test_the_tonnes_column_is_found_by_name_not_position(self):
        head = ["Date", "Tonnes of Gold"]
        blob = _xlsx([head, ["15-Oct-2026", 1000.0]])
        assert pos.gld_series(blob) == [("2026-10-15", 1000.0)]

    def test_a_workbook_without_the_column_is_empty(self):
        assert pos.gld_series(_xlsx([["Date", "Price"], ["15-Oct-2026", 1.0]])) == []

    def test_not_a_workbook_is_empty(self):
        assert pos.gld_series(b"%PDF-1.5 not a zip") == []

    def test_summary_changes(self):
        series = [(f"2026-09-{d:02d}", 1000.0 + d) for d in range(1, 31)]
        got = pos.gld_summary(series)

        assert got["date"] == "2026-09-30"
        assert got["tonnes"] == pytest.approx(1030.0)
        assert got["change_1d"] == pytest.approx(1.0)
        assert got["change_5d"] == pytest.approx(5.0)
        assert got["change_20d"] == pytest.approx(20.0)
        assert len(got["series"]) <= pos.SERIES_DAYS

    def test_a_short_series_has_no_long_change(self):
        got = pos.gld_summary([("2026-09-01", 1000.0), ("2026-09-02", 1001.0)])

        assert got["change_1d"] == pytest.approx(1.0)
        assert got["change_20d"] is None

    def test_empty_is_none(self):
        assert pos.gld_summary([]) is None


@pytest.fixture
def sources(monkeypatch, tmp_path):
    state = {"cot_calls": 0, "gld_calls": 0, "cot_raises": None, "gld_raises": None,
             "cot": [_cot_row("2026-10-13", 124_747, 17_658),
                     _cot_row("2026-10-06", 127_101, 16_114)],
             "gld": _xlsx([_HEAD, _day("15-Oct-2026", 1053.91), _day("16-Oct-2026", 1059.04)])}

    def _cot():
        state["cot_calls"] += 1
        if state["cot_raises"]:
            raise state["cot_raises"]
        return state["cot"]

    def _gld():
        state["gld_calls"] += 1
        if state["gld_raises"]:
            raise state["gld_raises"]
        return state["gld"]

    monkeypatch.setattr(pos, "_fetch_cot_rows", _cot)
    monkeypatch.setattr(pos, "_fetch_gld_bytes", _gld)
    state["path"] = tmp_path / "positioning.json"
    return state


@pytest.mark.asyncio
class TestSnapshot:
    async def test_a_cold_read_fetches_both_and_keeps_them(self, sources):
        got = await pos.snapshot(sources["path"], now=1_000_000.0)

        assert got["cot"]["report_date"] == "2026-10-13"
        assert got["gld"]["tonnes"] == pytest.approx(1059.04)
        assert got["cot_error"] is None and got["gld_error"] is None
        assert json.loads(sources["path"].read_text())["cot"]["data"]["mm_net"] == 107_089

    async def test_a_fresh_cache_is_served_without_fetching(self, sources):
        await pos.snapshot(sources["path"], now=1_000_000.0)
        await pos.snapshot(sources["path"], now=1_000_000.0 + 3600)

        assert sources["cot_calls"] == 1
        assert sources["gld_calls"] == 1

    async def test_a_stale_cache_is_refetched(self, sources):
        await pos.snapshot(sources["path"], now=1_000_000.0)
        await pos.snapshot(sources["path"], now=1_000_000.0 + pos.COT_TTL_S + 1)

        assert sources["cot_calls"] == 2
        assert sources["gld_calls"] == 2

    async def test_a_failed_fetch_serves_the_old_numbers_and_says_so(self, sources):
        await pos.snapshot(sources["path"], now=1_000_000.0)
        sources["cot_raises"] = RuntimeError("cftc down")
        later = 1_000_000.0 + pos.COT_TTL_S + 1

        got = await pos.snapshot(sources["path"], now=later)

        assert got["cot"]["report_date"] == "2026-10-13"
        assert "cftc down" in got["cot_error"]
        assert got["cot_fetched_at"] == 1_000_000.0
        assert got["gld_error"] is None

    async def test_a_failure_is_not_retried_on_every_read(self, sources):
        sources["cot_raises"] = RuntimeError("cftc down")
        await pos.snapshot(sources["path"], now=1_000_000.0)
        await pos.snapshot(sources["path"], now=1_000_000.0 + 60)

        assert sources["cot_calls"] == 1

    async def test_a_failure_is_retried_after_the_backoff(self, sources):
        sources["cot_raises"] = RuntimeError("cftc down")
        await pos.snapshot(sources["path"], now=1_000_000.0)
        await pos.snapshot(sources["path"], now=1_000_000.0 + pos.RETRY_S + 1)

        assert sources["cot_calls"] == 2

    async def test_an_unreadable_workbook_is_an_error_not_an_empty_success(self, sources):
        sources["gld"] = b"%PDF-1.5"

        got = await pos.snapshot(sources["path"], now=1_000_000.0)

        assert got["gld"] is None
        assert got["gld_error"]

    async def test_a_corrupt_cache_file_is_refetched(self, sources):
        sources["path"].write_text("{not json")

        got = await pos.snapshot(sources["path"], now=1_000_000.0)

        assert got["cot"]["report_date"] == "2026-10-13"
