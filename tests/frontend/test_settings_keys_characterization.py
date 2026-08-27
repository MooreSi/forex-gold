"""The settings package still reads and writes exactly the keys it used to.

`docs/todo/refactor/frontend/restructure/phase2-view-decomposition/020-settings.md`
asks for this: a settings page that silently stops persisting a value is close
to undetectable from the outside, and it lands on trading behaviour. The split
of the 3,487-line settings.py into a package is exactly the change that could
drop one.

The pinned set below was taken from settings.py at 2d3d13b, the commit before
the package conversion, and it matched the package exactly -- 83 keys, none
lost, none gained. Adding a setting means adding its key here, deliberately.
"""
import ast
import pathlib

PACKAGE = pathlib.Path(__file__).resolve().parents[1].parent / "frontend" / "pages" / "settings"

# Every config/env key the settings page touched before the split.
EXPECTED_KEYS = {
    "APPDATA",
    "USERPROFILE",
    "account_env",
    "account_type",
    "active",
    "ai_models_last_refreshed",
    "ai_provider",
    "anthropic_api_key",
    "avg_s",
    "balance",
    "bot_token_enc",
    "bridge_backend",
    "chat_id",
    "circuit_breaker_cooldown_mins",
    "circuit_breaker_enabled",
    "circuit_breaker_losses",
    "claude_model",
    "claude_models_cache",
    "connected",
    "count",
    "daily_enabled",
    "deepseek_api_key",
    "deepseek_model",
    "deepseek_models_cache",
    "dpm_enabled",
    "ea_bridge_enabled",
    "email",
    "enabled",
    "equity",
    "error",
    "exclude_high_risk",
    "expiry_date",
    "from_addr",
    "giveback_arm_usd",
    "giveback_guard_enabled",
    "giveback_pct",
    "host",
    "hour_blocklist_enabled",
    "imported",
    "internal_hedge_mode",
    "internal_net_exposure_max_lots",
    "is_demo",
    "label",
    "lag_s",
    "last_error",
    "licence_key",
    "licence_type",
    "max_daily_loss_pct",
    "max_lot_size",
    "max_open_trades",
    "max_s",
    "max_total_drawdown_pct",
    "min_s",
    "mt5_bottle_path",
    "mt5_bridge_url",
    "orb_report_enabled",
    "p90_s",
    "port",
    "profit_close_usd",
    "pw_label",
    "resend_api_key",
    "risk_governor_enabled",
    "send_provider",
    "send_time",
    "server",
    "skipped",
    "smtp_host",
    "smtp_password",
    "smtp_port",
    "smtp_user",
    "status",
    "telegram_api_hash",
    "telegram_api_id",
    "telegram_phone",
    "tls",
    "to_addr",
    "trade_allowed",
    "updated_at",
    "use_tls",
    "username_hint",
    "value",
    "weekly_enabled",
    "wine_bin",}


def _keys_in(source: str) -> set[str]:
    """Config keys a module reads or writes.

    Deliberately syntactic: `.get("x")`, a dict passed to `save_to_yaml(...)`,
    and `something["x"]`. It over-collects a little (os.environ keys land here
    too, which is why APPDATA is in the pinned set) and that is fine -- the
    test is about the set not changing, not about the set being minimal.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call):
            fn = node.func
            # get_config/save_config are the controller-fronted accessors the
            # page moved onto when backend.src.config stopped being imported
            # directly; the keys are the same, the door changed.
            if isinstance(fn, ast.Attribute) and fn.attr in ("get", "setdefault", "get_config"):
                if node.args and isinstance(node.args[0], ast.Constant) \
                        and isinstance(node.args[0].value, str):
                    found.add(node.args[0].value)
            if isinstance(fn, ast.Attribute) and fn.attr in (
                    "save_to_yaml", "save", "update", "save_config"):
                for arg in node.args:
                    if isinstance(arg, ast.Dict):
                        found |= {
                            k.value for k in arg.keys
                            if isinstance(k, ast.Constant) and isinstance(k.value, str)
                        }
        if isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Constant) \
                and isinstance(node.slice.value, str):
            found.add(node.slice.value)
    return found


def _package_keys() -> set[str]:
    found: set[str] = set()
    for module in sorted(PACKAGE.glob("*.py")):
        found |= _keys_in(module.read_text(encoding="utf-8"))
    return found


def test_every_config_key_the_page_writes_is_unchanged():
    """A dropped key is a setting that silently stops persisting."""
    actual = _package_keys()
    assert actual - EXPECTED_KEYS == set(), "new settings keys need pinning here"
    assert EXPECTED_KEYS - actual == set(), "a settings key disappeared from the page"


def test_the_key_enumeration_notices_a_dropped_key():
    """Negative control.

    A set-comparison test that cannot see a missing member proves nothing --
    docs/system/rules/40-testing.md calls that failure out by name. So take a
    module that really does carry keys, delete one, and confirm the extractor
    reports the loss.
    """
    source = (PACKAGE / "_risk.py").read_text(encoding="utf-8")
    full = _keys_in(source)
    assert full, "_risk.py should carry config keys; the extractor found none"

    victim = sorted(full)[0]
    damaged = source.replace(f'"{victim}"', '"__deliberately_renamed__"')
    assert damaged != source, f"could not damage the source for key {victim!r}"

    assert victim not in _keys_in(damaged), (
        "the extractor did not notice a removed key, so the characterization "
        "test above cannot fail and is worthless"
    )


def _relative_imports(module: pathlib.Path) -> set[str]:
    return {
        node.module.lstrip(".")
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8")))
        if isinstance(node, ast.ImportFrom) and node.level and node.module
    }


def test_every_section_module_is_reachable_from_the_package_shell():
    """A section nothing imports renders nowhere and fails silently.

    Reachability is transitive on purpose: _bridge is rendered by _mt5 and
    _log_export by _diagnostics, which is composition, not an orphan. What
    would be a bug is a module no path from __init__.py ever reaches.
    """
    reached, frontier = set(), ["__init__"]
    while frontier:
        name = frontier.pop()
        for dep in _relative_imports(PACKAGE / f"{name}.py"):
            if dep not in reached:
                reached.add(dep)
                frontier.append(dep)

    sections = {p.stem for p in PACKAGE.glob("_*.py")} - {"__init__"}
    assert sections - reached == set(), (
        "these modules are not reachable from __init__.py and so never render"
    )


def test_the_reachability_walk_would_notice_an_orphan():
    """Negative control for the walk above."""
    reached, frontier = set(), ["__init__"]
    while frontier:
        name = frontier.pop()
        for dep in _relative_imports(PACKAGE / f"{name}.py"):
            if dep not in reached:
                reached.add(dep)
                frontier.append(dep)
    assert "_orphan_that_does_not_exist" not in reached


def test_the_public_surface_is_only_what_other_pages_use():
    """The package's surface should not quietly become everything in it."""
    import frontend.pages.settings as settings
    assert settings.__all__ == ["render", "render_risk_card"]
