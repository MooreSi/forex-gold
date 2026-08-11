"""Data loaders for the lab. Read-only access to the .db snapshots in _shared/data.

Every function returns a pandas DataFrame with epoch columns converted to
UTC datetimes (suffix `_dt`), keeping the raw epoch column too.

Source-of-truth notes (verified 2026-08-11 against the 21-31 Jul snapshots):
- reversal_engine.db timestamps are epoch floats (created_at, ts, ...).
- forex_trader_demo.db mixes epoch ints (consolidated_trades.open_time)
  and ISO strings (telegram_messages.timestamp).
- re_signals.ml_features_json is a JSON list of 24 floats.
- Sizing is risk-based (sl_risk_usd ~= $50), so cross-strategy comparisons
  should use R-multiples (pnl / risk), not dollars.
"""
from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pandas as pd

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
RE_DB = DATA_DIR / "reversal_engine.db"
MAIN_DB = DATA_DIR / "forex_trader_demo.db"

# Tables the lab must never read (credentials/config secrets live there).
FORBIDDEN_TABLES = {"mt5_credentials", "telegram_config", "email_config"}


def _connect(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)  # read-only open
    return con


def _read(path: Path, sql: str) -> pd.DataFrame:
    low = sql.lower()
    for t in FORBIDDEN_TABLES:
        if t in low:
            raise PermissionError(f"lab code must not read table {t!r}")
    with _connect(path) as con:
        return pd.read_sql_query(sql, con)


def _epoch_to_dt(df: pd.DataFrame, cols: list[str]) -> pd.DataFrame:
    for c in cols:
        if c in df.columns:
            df[c + "_dt"] = pd.to_datetime(df[c], unit="s", utc=True, errors="coerce")
    return df


def signals_df() -> pd.DataFrame:
    """All 741 reversal-engine signals with parsed features and day column."""
    df = _read(RE_DB, "SELECT * FROM re_signals ORDER BY id")
    df = _epoch_to_dt(df, ["created_at", "trigger_time", "close_time"])
    df["day"] = df["created_at_dt"].dt.strftime("%Y-%m-%d")
    df["hour_utc"] = df["created_at_dt"].dt.hour
    df["features"] = df["ml_features_json"].map(
        lambda s: json.loads(s) if isinstance(s, str) else None
    )
    return df


def prices_df() -> pd.DataFrame:
    """~60-second price series recovered from re_analysis_log (price > 0).

    COARSE: median gap 60s, gaps overnight/weekend. Anything that needs
    intrabar precision (tight TP ladders) needs the MT5 M1 export instead.
    """
    df = _read(
        RE_DB,
        "SELECT ts, price, atr, adx, session, htf_bias FROM re_analysis_log "
        "WHERE price > 0 ORDER BY ts",
    )
    return _epoch_to_dt(df, ["ts"])


def gdc_signals_df() -> pd.DataFrame:
    """The older gdc_* incarnation of the engine (same schema idea)."""
    df = _read(RE_DB, "SELECT * FROM gdc_signals ORDER BY id")
    return _epoch_to_dt(df, ["created_at", "trigger_time", "close_time"])


def consolidated_trades_df() -> pd.DataFrame:
    """Whole-app trade ledger. WARNING: contains probable duplicate rows
    (same pnl seconds apart) — see 001-review.md §4.4. dedup_trades() below."""
    df = _read(MAIN_DB, "SELECT * FROM consolidated_trades ORDER BY id")
    for c in ("open_time", "close_time", "received_at"):
        df[c] = pd.to_numeric(df[c], errors="coerce")
    return _epoch_to_dt(df, ["open_time", "close_time"])


def dedup_trades(df: pd.DataFrame, window_s: int = 30) -> pd.DataFrame:
    """Drop rows that look like sync-echoes: same engine/direction/pnl with
    close_time within `window_s` of a kept row. Conservative first pass."""
    df = df.sort_values("close_time").copy()
    keep, last_seen = [], {}
    for _, r in df.iterrows():
        key = (r.get("engine"), r.get("direction"), round(float(r.get("pnl_dollars") or 0), 2))
        t = float(r.get("close_time") or 0)
        prev = last_seen.get(key)
        if prev is not None and 0 <= t - prev <= window_s:
            keep.append(False)
        else:
            keep.append(True)
            last_seen[key] = t
    return df[pd.Series(keep, index=df.index)]


def tg_channel_signals_df() -> pd.DataFrame:
    """Signals parsed from the Telegram channels (the thing RE tries to predict)."""
    df = _read(MAIN_DB, "SELECT * FROM vantage_tg_signals ORDER BY id")
    return df


def telegram_messages_df() -> pd.DataFrame:
    df = _read(
        MAIN_DB,
        "SELECT id, telegram_message_id, group_name, timestamp, text "
        "FROM telegram_messages ORDER BY id",
    )
    df["timestamp_dt"] = pd.to_datetime(df["timestamp"], utc=True, errors="coerce")
    return df
