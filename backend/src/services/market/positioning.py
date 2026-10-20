"""Who holds gold: CFTC Commitments of Traders and GLD holdings (docs/todo/024).

Two reads gold desks watch. Display only: no gate, engine, sizing rule or
prompt reads anything here.

**COT.** The CFTC's public reporting API (Socrata), Disaggregated Futures
Only, dataset `72hh-3qpy`, COMEX gold `cftc_contract_market_code = '088691'`
(verified 2026-10-20). Managed money is the speculative money (funds, CTAs);
when it is heavily net long the crowd is already in, which is when sharp
flushes happen. Positions are as of Tuesday and published on Friday, so the
figure is always a few days old. The COT index puts this week's net between
its lowest (0) and highest (100) of the last three years.

**GLD.** SPDR Gold Trust's daily archive, an xlsx with a "Tonnes of Gold"
column (verified 2026-10-20; the old CSV path now redirects to a PDF). Read
with `zipfile` and the XML parser, not a spreadsheet library: one column of
one sheet does not justify a new dependency. The file says it is not to be
redistributed; it is read for display on this machine and never stored
beyond the summary below.

Fetched off the event loop, cached in `DATA_DIR/positioning.json`, and never
on the trading path. A failed fetch keeps serving the last good numbers with
their age and the error, and is not retried for `RETRY_S`.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import time
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional
from xml.etree import ElementTree as ET

import httpx

log = logging.getLogger(__name__)

__all__ = ["snapshot"]

COT_URL = "https://publicreporting.cftc.gov/resource/72hh-3qpy.json"
COT_GOLD_CODE = "088691"
GLD_URL = ("https://api.spdrgoldshares.com/api/v1/historical-archive"
           "?product=gld&exchange=NYSE&lang=en")

COT_TTL_S = 12 * 3600
GLD_TTL_S = 6 * 3600
RETRY_S = 30 * 60
_TIMEOUT_S = 30.0
INDEX_YEARS = 3
SERIES_WEEKS = 52
SERIES_DAYS = 90
# Three years of weeks, plus slack for the odd missing week.
_COT_ROWS = 170

_lock = asyncio.Lock()


# -- COT ---------------------------------------------------------------------

def _int(v) -> Optional[int]:
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return None


def _cot_weeks(rows: list[dict]) -> list[dict]:
    out = {}
    for r in rows or []:
        try:
            d = date.fromisoformat(str(r.get("report_date_as_yyyy_mm_dd", ""))[:10])
        except ValueError:
            continue
        long_, short = _int(r.get("m_money_positions_long_all")), _int(r.get("m_money_positions_short_all"))
        if long_ is None or short is None:
            continue
        out[d] = {"date": d, "long": long_, "short": short, "net": long_ - short,
                  "oi": _int(r.get("open_interest_all"))}
    return [out[d] for d in sorted(out)]


def cot_summary(rows: list[dict]) -> Optional[dict]:
    """Managed money in COMEX gold for the latest report, or None."""
    weeks = _cot_weeks(rows)
    if not weeks:
        return None
    now = weeks[-1]
    since = now["date"] - timedelta(days=365 * INDEX_YEARS)
    window = [w["net"] for w in weeks if w["date"] >= since]
    lo, hi = min(window), max(window)
    index = 50.0 if hi == lo else (now["net"] - lo) / (hi - lo) * 100.0
    prev = weeks[-2] if len(weeks) > 1 else None
    return {
        "report_date": now["date"].isoformat(),
        "mm_long": now["long"],
        "mm_short": now["short"],
        "mm_net": now["net"],
        "net_change": now["net"] - prev["net"] if prev else None,
        "net_pct_oi": round(now["net"] / now["oi"] * 100.0, 1) if now["oi"] else None,
        "open_interest": now["oi"],
        "cot_index": round(index, 1),
        "weeks": len(window),
        "series": [{"date": w["date"].isoformat(), "net": w["net"]}
                   for w in weeks[-SERIES_WEEKS:]],
    }


def _fetch_cot_rows() -> list[dict]:
    params = {
        "$select": ("report_date_as_yyyy_mm_dd,open_interest_all,"
                    "m_money_positions_long_all,m_money_positions_short_all"),
        "$where": f"cftc_contract_market_code='{COT_GOLD_CODE}'",
        "$order": "report_date_as_yyyy_mm_dd DESC",
        "$limit": str(_COT_ROWS),
    }
    r = httpx.get(COT_URL, params=params, timeout=_TIMEOUT_S)
    r.raise_for_status()
    return r.json()


# -- GLD ---------------------------------------------------------------------

_NS = re.compile(r"^\{[^}]*\}")


def _tag(el) -> str:
    return _NS.sub("", el.tag)


def _col(ref: str) -> str:
    return re.sub(r"\d", "", ref or "")


def _shared_strings(z: zipfile.ZipFile) -> list[str]:
    try:
        root = ET.fromstring(z.read("xl/sharedStrings.xml"))
    except KeyError:
        return []
    out = []
    for si in root:
        out.append("".join(t.text or "" for t in si.iter() if _tag(t) == "t"))
    return out


def _rows(z: zipfile.ZipFile, name: str, strings: list[str]):
    root = ET.fromstring(z.read(name))
    for row in root.iter():
        if _tag(row) != "row":
            continue
        cells = {}
        for c in row:
            if _tag(c) != "c":
                continue
            v = next((x.text for x in c if _tag(x) == "v"), None)
            if v is None:
                continue
            if c.get("t") == "s":
                try:
                    v = strings[int(v)]
                except (ValueError, IndexError):
                    continue
            cells[_col(c.get("r", ""))] = v
        yield cells


def gld_series(blob: bytes) -> list[tuple[str, float]]:
    """[(iso date, tonnes)] oldest first. [] for anything that is not the
    archive workbook, so a changed format reads as an error, not as zero."""
    try:
        z = zipfile.ZipFile(io.BytesIO(blob))
    except zipfile.BadZipFile:
        return []
    strings = _shared_strings(z)
    sheets = sorted(n for n in z.namelist() if re.match(r"xl/worksheets/sheet\d+\.xml$", n))
    for name in sheets:
        date_col = tonnes_col = None
        out: dict[str, float] = {}
        for cells in _rows(z, name, strings):
            if date_col is None:
                by_name = {str(v).strip(): k for k, v in cells.items()}
                if "Date" in by_name and "Tonnes of Gold" in by_name:
                    date_col, tonnes_col = by_name["Date"], by_name["Tonnes of Gold"]
                continue
            try:
                d = datetime.strptime(str(cells.get(date_col, "")).strip(), "%d-%b-%Y").date()
                t = float(cells.get(tonnes_col))
            except (TypeError, ValueError):
                continue
            out[d.isoformat()] = t
        if date_col is not None:
            return sorted(out.items())
    return []


def gld_summary(series: list[tuple[str, float]]) -> Optional[dict]:
    if not series:
        return None
    last_date, last = series[-1]

    def change(n: int) -> Optional[float]:
        return round(last - series[-1 - n][1], 2) if len(series) > n else None

    return {
        "date": last_date,
        "tonnes": round(last, 2),
        "change_1d": change(1),
        "change_5d": change(5),
        "change_20d": change(20),
        "series": [{"date": d, "tonnes": round(t, 2)} for d, t in series[-SERIES_DAYS:]],
    }


def _fetch_gld_bytes() -> bytes:
    r = httpx.get(GLD_URL, timeout=_TIMEOUT_S, follow_redirects=True,
                  headers={"User-Agent": "Mozilla/5.0"})
    r.raise_for_status()
    return r.content


# -- Cache -------------------------------------------------------------------

def _load(path: Path) -> dict:
    try:
        blob = json.loads(path.read_text(encoding="utf-8"))
        return blob if isinstance(blob, dict) else {}
    except (OSError, ValueError):
        return {}


def _save(path: Path, blob: dict) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(blob), encoding="utf-8")
        tmp.replace(path)
    except OSError as exc:
        log.debug("[positioning] cache write failed: %s", exc)


def _due(entry: dict, ttl: float, now: float) -> bool:
    if entry.get("failed_at") and now - float(entry["failed_at"]) < RETRY_S:
        return False
    return not entry.get("data") or now - float(entry.get("fetched_at") or 0) >= ttl


def _cot_now() -> dict:
    got = cot_summary(_fetch_cot_rows())
    if got is None:
        raise ValueError("CFTC returned no gold rows")
    return got


def _gld_now() -> dict:
    got = gld_summary(gld_series(_fetch_gld_bytes()))
    if got is None:
        raise ValueError("GLD archive had no Date / Tonnes of Gold rows")
    return got


async def _refresh(entry: dict, fn, now: float) -> dict:
    try:
        data = await asyncio.to_thread(fn)
        return {"data": data, "fetched_at": now, "error": None, "failed_at": None}
    except Exception as exc:
        log.info("[positioning] %s failed: %s", getattr(fn, "__name__", "fetch"), exc)
        return {**entry, "error": str(exc) or type(exc).__name__, "failed_at": now}


def _default_path() -> Path:
    from backend.src.config import DATA_DIR
    return Path(DATA_DIR) / "positioning.json"


async def snapshot(path: Optional[Path] = None, now: Optional[float] = None) -> dict[str, Any]:
    """The latest COT and GLD summaries, fetching whichever is stale."""
    path = Path(path) if path else _default_path()
    now = time.time() if now is None else float(now)
    async with _lock:
        blob = _load(path)
        cot, gld = blob.get("cot") or {}, blob.get("gld") or {}
        changed = False
        if _due(cot, COT_TTL_S, now):
            cot, changed = await _refresh(cot, _cot_now, now), True
        if _due(gld, GLD_TTL_S, now):
            gld, changed = await _refresh(gld, _gld_now, now), True
        if changed:
            _save(path, {"cot": cot, "gld": gld})
    return {
        "cot": cot.get("data"), "cot_fetched_at": cot.get("fetched_at"),
        "cot_error": cot.get("error"),
        "gld": gld.get("data"), "gld_fetched_at": gld.get("fetched_at"),
        "gld_error": gld.get("error"),
    }
