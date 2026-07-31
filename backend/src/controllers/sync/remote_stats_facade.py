"""
Facades exposing the same function names as each signal engine's `database`,
`ml_engine`, and `adaptive_params` modules — transparently switching between
this node's own local data and the mirrored remote-VPS snapshot based on the
app's Local/Remote mode, so the Breakout/Bounce/Reversal Engine panel rendering code
needs no changes at any individual call site.

Per-signal helpers with no remote equivalent (get_ml_features_for_signal,
ood_distance — used only for the active-positions OOD-distance column)
always delegate to the local model; there's no mirrored data for those.
"""
from __future__ import annotations


def _is_centralized_remote_mode() -> bool:
    """True only on the node that is actually generating locally under
    centralized signal generation with the VPS as active trader — i.e. the
    Mac, never the VPS itself. On the VPS, centralized_signal_gen_enabled
    and active_trader==remote_vps are BOTH true too, but the VPS is the one
    whose own engines were centralized OUT (should_generate_signals_here()
    is False there) — it must never be labeled "Local" or treated as the
    generation owner just because the shared setting reads true."""
    try:
        from backend.src.db import database as db_module
        from backend.src.controllers.sync import server as _sync_srv_mod
        from backend.src.controllers.sync.protocol import TRADER_REMOTE_VPS
        rs = db_module.get_risk_settings()
        if not rs.get("centralized_signal_gen_enabled"):
            return False
        if _sync_srv_mod.get_instance() is not None:
            return False  # this is the VPS — its engines are centralized out, not "local"
        return db_module.get_active_trader() == TRADER_REMOTE_VPS
    except Exception:
        return False


def _is_remote_active() -> bool:
    """True when this node should show the VPS's own signal-generator
    progress instead of its own local engine's.

    Under centralized signal generation (Settings > Remote Node), this is
    always False even in Remote mode: generation has moved to this node, so
    its own engines' data/state is what's actually current — the VPS's
    engines have stopped analyzing entirely (see
    core.database.should_generate_signals_here), and showing/controlling
    them here would be showing stale, permanently-idle data."""
    if _is_centralized_remote_mode():
        return False
    try:
        from backend.src.controllers.sync import client as sync_client
        cli = sync_client.get_instance()
        return (
            cli.conn_state == "connected"
            and cli.remote_status.get("active_trader") == "remote_vps"
            and bool(cli.remote_signal_gen_stats)
        )
    except Exception:
        return False


def _remote_engine_stats(key: str) -> dict:
    from backend.src.controllers.sync import client as sync_client
    return sync_client.get_instance().remote_signal_gen_stats.get(key, {})


class _DbFacade:
    def __init__(self, key: str, real_module):
        self._key = key
        self._real = real_module

    def _remote(self) -> dict:
        return _remote_engine_stats(self._key)

    def get_virtual_balance(self) -> float:
        if _is_remote_active():
            return self._remote().get("virtual_balance", 0.0)
        return self._real.get_virtual_balance()

    def get_max_drawdown(self) -> float:
        if _is_remote_active():
            return self._remote().get("max_drawdown", 0.0)
        return self._real.get_max_drawdown()

    def get_stats(self) -> dict:
        if _is_remote_active():
            return self._remote().get("stats", {})
        return self._real.get_stats()

    def get_open_signals(self) -> list:
        if _is_remote_active():
            return self._remote().get("open_signals", [])
        return self._real.get_open_signals()

    def get_all_signals(self, limit: int = 100) -> list:
        if _is_remote_active():
            return self._remote().get("all_signals", [])
        return self._real.get_all_signals(limit=limit)

    def get_perf_by_breakout_type(self) -> list:
        if _is_remote_active():
            return self._remote().get("perf_by_breakout_type", [])
        return self._real.get_perf_by_breakout_type()

    def get_perf_by_adx_band(self) -> list:
        if _is_remote_active():
            return self._remote().get("perf_by_adx_band", [])
        return self._real.get_perf_by_adx_band()

    def get_perf_by_session(self) -> list:
        if _is_remote_active():
            return self._remote().get("perf_by_session", [])
        return self._real.get_perf_by_session()

    def get_perf_by_bias(self) -> list:
        if _is_remote_active():
            return self._remote().get("perf_by_bias", [])
        return self._real.get_perf_by_bias()

    def get_perf_by_level_type(self) -> list:
        if _is_remote_active():
            return self._remote().get("perf_by_level_type", [])
        return self._real.get_perf_by_level_type()

    def get_perf_by_regime(self) -> list:
        if _is_remote_active():
            return self._remote().get("perf_by_regime", [])
        return self._real.get_perf_by_regime()

    def get_consecutive_losses(self) -> int:
        if _is_remote_active():
            return self._remote().get("consecutive_losses", 0)
        return self._real.get_consecutive_losses()

    def get_correlation_history(self, days: int = 7) -> list:
        if _is_remote_active():
            return self._remote().get("correlation_history", [])
        return self._real.get_correlation_history(days=days)

    def get_active_levels(self) -> list:
        if _is_remote_active():
            return self._remote().get("active_levels", [])
        return self._real.get_active_levels()

    def get_ml_features_for_signal(self, signal_id):
        return self._real.get_ml_features_for_signal(signal_id)

    def __getattr__(self, name):
        # Anything not explicitly mirrored above (e.g. write actions like
        # reset/update calls the panels also make on this same alias)
        # always acts on this node's own local data.
        return getattr(self._real, name)


class _MlFacade:
    def __init__(self, key: str, real_module):
        self._key = key
        self._real = real_module

    def summary(self) -> dict:
        if _is_remote_active():
            return _remote_engine_stats(self._key).get("ml_summary", {})
        return self._real.summary()

    def get_ml_metrics(self) -> dict:
        if _is_remote_active():
            return _remote_engine_stats(self._key).get("ml_metrics", {})
        return self._real.get_ml_metrics()

    def ood_distance(self, features):
        return self._real.ood_distance(features)

    def __getattr__(self, name):
        return getattr(self._real, name)


class _ParamsFacade:
    def __init__(self, key: str, real_module):
        self._key = key
        self._real = real_module

    def get_all(self) -> dict:
        if _is_remote_active():
            return _remote_engine_stats(self._key).get("params", {})
        return self._real.get_all()

    def get_regime_overrides(self) -> dict:
        if _is_remote_active():
            return _remote_engine_stats(self._key).get("regime_overrides", {})
        return self._real.get_regime_overrides()

    def __getattr__(self, name):
        return getattr(self._real, name)


def make_facades(key: str, db_module, ml_module=None, params_module=None):
    """Return (db_facade, ml_facade, params_facade) for one engine.
    Pass None for ml_module/params_module if that engine doesn't have one
    (Reversal Engine has no adaptive_params)."""
    db_facade = _DbFacade(key, db_module)
    ml_facade = _MlFacade(key, ml_module) if ml_module is not None else None
    params_facade = _ParamsFacade(key, params_module) if params_module is not None else None
    return db_facade, ml_facade, params_facade
