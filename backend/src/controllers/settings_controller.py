"""Settings page + app-shell API: app config, credentials, expert tunables."""
from __future__ import annotations

from typing import Optional

from backend.src.services.analytics import pnl as _pnl
from backend.src.services.broker import credentials as _credentials
from backend.src.services.health import log_events as _log_events
from backend.src.services.cluster import node as _node
from backend.src.services.notifications import config as _notify
from backend.src.services.risk import app_config as _config
from backend.src.services.risk import retention as _retention
from backend.src.services.risk import settings as _risk
import backend.src.config as _cfg_file

__all__ = [
    "get_app_config", "get_app_config_async", "set_app_config", "get_risk_settings",
    "update_risk_settings", "get_active_trader", "set_active_trader",
    "get_email_config", "save_email_config", "get_telegram_config",
    "save_telegram_config", "get_mt5_credentials", "save_mt5_credentials",
    "sync_bridge_credentials_file", "get_data_retention_days",
    "set_data_retention_days", "reset_circuit_breaker",
    "get_circuit_breaker_state", "switch_environment_db",
    "fetch_signal_execution_lags", "fetch_realised_pnl_last_24h",
    "live_log_lines",
    "get_expert_param_catalogue", "save_expert_params",
    "reset_expert_param", "reset_all_expert_params",
    "load_config", "get_config", "save_config", "is_debug",
    "DATA_DIR", "USER_DATA_DIR",
]


def get_app_config(key: str) -> Optional[str]:
    return _config.get(key)


async def get_app_config_async(key: str) -> Optional[str]:
    return await _config.get_async(key)


def set_app_config(key: str, value: str) -> None:
    _config.set(key, value)


def get_risk_settings() -> dict:
    return _risk.get()


def update_risk_settings(fields: dict) -> None:
    _risk.update(fields)


def get_active_trader() -> str:
    return _node.get_active_trader()


def set_active_trader(value: str, *args, **kwargs):
    return _node.set_active_trader(value, *args, **kwargs)


# -- Credentials and notification config -------------------------------------

def get_email_config() -> dict:
    return _notify.get_email()


def save_email_config(*args, **kwargs):
    return _notify.save_email(*args, **kwargs)


def get_telegram_config() -> dict:
    return _notify.get_telegram()


def save_telegram_config(*args, **kwargs):
    return _notify.save_telegram(*args, **kwargs)


def get_mt5_credentials(*args, **kwargs):
    return _credentials.get_mt5(*args, **kwargs)


def save_mt5_credentials(*args, **kwargs):
    return _credentials.save_mt5(*args, **kwargs)


def sync_bridge_credentials_file(*args, **kwargs):
    return _credentials.sync_bridge_file(*args, **kwargs)


# -- Retention, breaker, environment ------------------------------------------

def get_data_retention_days() -> int:
    return _retention.get_days()


def set_data_retention_days(days: int) -> None:
    _retention.set_days(days)


def reset_circuit_breaker(*args, **kwargs):
    return _risk.reset_circuit_breaker(*args, **kwargs)


def get_circuit_breaker_state() -> dict:
    return _risk.circuit_breaker_state()


async def get_circuit_breaker_state_async() -> dict:
    return await _risk.circuit_breaker_state_async()


def switch_environment_db(db_path: str) -> None:
    """Re-point the shared connection at another environment's DB file."""
    _retention.switch_environment(db_path)


def fetch_signal_execution_lags(db_path: str) -> list:
    return _pnl.signal_execution_lags(db_path)


def fetch_realised_pnl_last_24h(cutoff: float) -> float:
    return _pnl.realised_pnl_last_24h(cutoff)


async def live_log_lines() -> list[tuple[str, str]]:
    """Meaningful log events since the last app start, scanned off-loop."""
    return await _log_events.since_last_start_async()


# -- Expert Tunables ---------------------------------------------------------
# The page renders whatever this hands back and interprets none of it, so a
# new tunable appears in the UI by being added to the catalogue -- no bespoke
# widget, ever. Clamping and unknown-key rejection happen in the service, not
# here: a stale page or a direct call must not be able to bypass them.

def get_expert_param_catalogue() -> dict:
    from backend.src.services.risk import expert_params
    return expert_params.catalogue()


def save_expert_params(values: dict) -> dict:
    from backend.src.services.risk import expert_params
    return expert_params.set_params(values)


def reset_expert_param(key: str) -> dict:
    from backend.src.services.risk import expert_params
    return expert_params.reset(key)


def reset_all_expert_params() -> dict:
    from backend.src.services.risk import expert_params
    return expert_params.reset_all()

# -- App config (the YAML file, distinct from the app_config DB table above) --
#
# The frontend used to import backend.src.config in eight of its fifteen source
# units, which CLAUDE.md calls out by name as counting against the
# controller-boundary contract. It only ever needed these four things.

def load_config() -> dict:
    return _cfg_file.load()


def get_config(key: str, default=None):
    return _cfg_file.get(key, default)


def save_config(values: dict) -> None:
    _cfg_file.save_to_yaml(values)


def is_debug() -> bool:
    return _cfg_file.is_debug()

# The two data-directory paths pages need for log export, diagnostics and the
# EA file drop. Constants, re-exported for the same reason as the config
# accessors above.
DATA_DIR = _cfg_file.DATA_DIR
USER_DATA_DIR = _cfg_file.USER_DATA_DIR


# ── Internal-engine exposure modes ───────────────────────────────────────────
# The vocabulary the Risk settings radio group offers. Constants rather than a
# service call, re-exported so the page does not reach into
# services.positions.core_internal_exposure_guard for three strings.
from backend.src.services.positions import (  # noqa: E402
    core_internal_exposure_guard as _ieg,
)

MODE_OFF = _ieg.MODE_OFF
MODE_NET_EXPOSURE = _ieg.MODE_NET_EXPOSURE
MODE_SELF_HEDGE = _ieg.MODE_SELF_HEDGE
