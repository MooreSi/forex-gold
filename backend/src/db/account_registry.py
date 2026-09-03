"""Which database file belongs to which MT5 account.

Owner, 2026-09-03: "if there is a fresh database for a new demo account all of
the risk settings, ea templates and credentials should remain, the account is
simply where the trades are executed".

Until now the database was split by ENVIRONMENT only -- `forex_trader_demo.db`
and `forex_trader_live.db` -- so two demo accounts shared one set of trades.
This splits by login as well.

**The rule that matters most is that nothing changes for an existing install.**
That file also holds 22 EA templates, the risk settings, the Telegram config
and the credentials; if this resolver ever hands back a new path for a login
already using `forex_trader_demo.db`, the app opens an empty file and 1,309
trades look like they vanished. So the first login seen for an environment
CLAIMS the existing default file. Upgrading is a no-op by construction; only
adding a second account creates anything.

A genuinely new account gets `forex_trader_<env>_<login>.db`, seeded with the
shared tables copied from the database it was created alongside -- so it opens
with your templates, your risk settings and working credentials, and no
trades.

Why copy rather than share one settings database: sharing means every repo
that touches those tables needs a second connection and a rule about which one
wins, which is a large change across the data layer for a two-account install.
Copying is one function, no schema change, and reversible -- the cost is that
a settings edit made later on account A does not follow account B. If that
becomes a problem, promoting the shared tables into their own database is the
next step, and this registry is where it would go.

Never raises. This decides which database the app opens; an exception here is
a dead app, and the environment default is always a safe answer.
"""
from __future__ import annotations

import json
import logging
import shutil
import sqlite3
from pathlib import Path
from typing import Optional

log = logging.getLogger(__name__)

REGISTRY_NAME = "accounts.json"

# Copied into a new account's database. Everything here describes the INSTALL
# -- how it trades, what it is connected to, what it has learned -- rather than
# what one account did.
SHARED_TABLES = (
    "mt5_credentials",          # without these the new account cannot connect
    "vantage_risk_settings",
    "ea_trade_templates",
    "custom_strategies",
    "strategy_param_templates",
    "telegram_config",
    "email_config",
    "channel_parser_config",
    "channel_learned_rules",
    "logic_keyword_lexicons",
    "channel_strategy_rec",
    "dpm_calibration",
    "app_config",
    "vantage_fee_settings",
)

# Deliberately NOT copied: vantage_simulated_trades, vantage_signals,
# vantage_partial_closes, vantage_pending_orders, vantage_simulation_account,
# consolidated_trades, vantage_ladder_legs, trade_spread_cache,
# channel_performance, dpm_trade_performance. Those are what the account did.
# A new account showing another account's trades is worse than showing none.


def login_for_env(creds: Optional[dict], env: str) -> str:
    """The MT5 login for `env`, as text, or "" if there is not one.

    demo and live keep their logins in different columns of the same row
    (`login` / `live_login`), so reading the wrong one would point a live
    account at the demo database.

    A 0 login counts as unset: the credentials table stores 0 for "not
    configured", and `forex_trader_demo_0.db` would be a real file for an
    account that does not exist.
    """
    try:
        field = "live_login" if env == "live" else "login"
        raw = (creds or {}).get(field)
        if raw is None:
            return ""
        text = str(raw).strip()
        if not text or text == "0":
            return ""
        return text
    except Exception:
        return ""


def _registry_path(data_dir: Path) -> Path:
    return Path(data_dir) / REGISTRY_NAME


def _load(data_dir: Path) -> dict:
    path = _registry_path(data_dir)
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return data
    except Exception as exc:
        log.warning("[accounts] registry unreadable (%s) — treating as empty. "
                    "The environment default is still used, so no data is at "
                    "risk.", exc)
    return {}


def _save(data_dir: Path, registry: dict) -> None:
    try:
        _registry_path(data_dir).write_text(
            json.dumps(registry, indent=2, sort_keys=True), encoding="utf-8")
    except Exception as exc:
        log.error("[accounts] could not write the registry (%s). The mapping "
                  "will be recomputed next start, which can hand a second "
                  "account the default file.", exc)


def default_db_path(data_dir: Path, env: str) -> Path:
    return Path(data_dir) / f"forex_trader_{env}.db"


def _table_names(conn: sqlite3.Connection) -> set:
    rows = conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()
    return {r[0] for r in rows}


def _seed_shared_tables(source: Path, target: Path) -> None:
    """Copy the install-wide tables from `source` into a fresh `target`.

    Whole-file copy then delete-the-rest, rather than a table-by-table INSERT:
    it carries the schema, indexes and migration stamp with it, so the new
    database is at the same schema version as the one it came from and needs
    no migration replay to be usable.
    """
    shutil.copy2(source, target)
    conn = sqlite3.connect(target)
    try:
        present = _table_names(conn)
        for name in sorted(present):
            if name in SHARED_TABLES or name.startswith("sqlite_"):
                continue
            if name == "schema_version":
                continue          # keep the stamp; the copy is at that version
            conn.execute(f"DELETE FROM {name}")
        conn.commit()
        conn.execute("VACUUM")
    finally:
        conn.close()


def resolve_db_path(data_dir: Path, env: str, login: Optional[str]) -> Path:
    """The database file for this environment and MT5 login.

    With no login -- first run, or credentials not entered yet -- the
    environment default is returned and nothing is recorded. Inventing a file
    for an account we cannot name would strand its data under a name nothing
    resolves to again.
    """
    data_dir = Path(data_dir)
    default = default_db_path(data_dir, env)

    login = (str(login).strip() if login is not None else "")
    if not login:
        return default

    try:
        registry = _load(data_dir)
        by_env = registry.get(env)
        if not isinstance(by_env, dict):
            by_env = {}

        known = by_env.get(login)
        if isinstance(known, str) and known:
            return data_dir / known

        # First login seen for this environment claims the existing default,
        # so an install that upgrades into this code keeps its database.
        if not by_env:
            by_env[login] = default.name
            registry[env] = by_env
            _save(data_dir, registry)
            return default

        target = data_dir / f"forex_trader_{env}_{login}.db"
        if not target.exists():
            source = data_dir / next(iter(by_env.values()))
            if source.exists():
                try:
                    _seed_shared_tables(source, target)
                except Exception as exc:
                    log.error("[accounts] could not seed %s from %s (%s). The "
                              "account still gets a database; it will start "
                              "empty and need its settings re-entered.",
                              target.name, source.name, exc)
        by_env[login] = target.name
        registry[env] = by_env
        _save(data_dir, registry)
        return target
    except Exception as exc:
        log.error("[accounts] could not resolve a database for %s/%s (%s) — "
                  "falling back to %s.", env, login, exc, default.name)
        return default
