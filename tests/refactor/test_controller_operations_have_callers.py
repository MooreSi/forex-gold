"""A controller operation exists to be called by something that is not a test.

The layering rule is `frontend/ → controllers/ → services/`, and a controller
is *"a flat `<name>_controller.py` that names an operation and forwards it to
one service"*. So every name a controller exports is a route the UI uses. One
that nothing in `backend/` or `frontend/` mentions is a route to nowhere.

The layer is in good shape: **252 of 255 exported operations are referenced.**
That is what makes this worth gating rather than baselining — three exceptions,
not three hundred.

**A test caller does not count, and `engines_running` is why.** It is exported,
it is defined, and `tests/controllers/test_engines_controller_lifecycle.py`
calls it — so the coverage report shows it green and the suite proves it works.
Nothing in the app has ever called it. A test that keeps dead code looking alive
is the exact shape this repo's rules were written after: *"an audit found ~3,000
lines of extracted code nothing called"*.

Shrink-only. The known-dead set may lose entries and must never gain one.
"""
from __future__ import annotations

import importlib
from pathlib import Path

import pytest

from tests.refactor._source_scan import REPO, references_to

_CONTROLLERS = sorted(
    p.as_posix() for p in (REPO / "backend/src/controllers").glob("*_controller.py")
)

# Waiting for a caller that the React port has not written yet.
#
# **This is not KNOWN_DEAD and must not be merged into it.** Dead means nobody
# wants it; these were each called by a NiceGUI tab that the big-bang replace
# on 2026-09-18 deleted before its React equivalent existed. The operation is
# unchanged and still tested; the tab that asks for it is task 080 in
# docs/todo/frontend/react-port/.
#
# The set is **shrink-only** and it is meant to reach zero. Every entry is one
# question a router will have to ask. If task 080 finishes and an entry is
# still here, that is the evidence it was genuinely dead all along — and then
# it becomes a delete, not a move into KNOWN_DEAD.
#
# It mirrors the `awaiting-react-port` class in
# tools/refactor_audit/orphan_module_allowlist.json: same cause, same debt,
# same removal condition.
AWAITING_REACT_PORT = {
    ("ai_analysis_controller", "gather_channel_data"),
    ("ai_analysis_controller", "gather_signal_generator_data"),
    ("ai_analysis_controller", "gather_strategy_dpm_data"),
    ("backtest_controller", "BROKER_TZ_OFFSET"),
    ("backtest_controller", "summarise_templates"),
    ("broker_controller", "BUILTIN_PRESET_NAME"),
    ("broker_controller", "ea_is_healthy"),
    ("broker_controller", "ea_seconds_since_last_seen"),
    ("dpm_controller", "get_calibration_rows"),
    ("dpm_controller", "get_calibration_runs"),
    ("dpm_controller", "get_perf_rows"),
    ("engines_controller", "start_stopped_engines"),
    ("engines_controller", "stop_running_engines"),
    ("engines_controller", "sub_engines"),
    ("history_controller", "ticket_group_map"),
    ("history_controller", "ticket_max_tp_map"),
    ("history_controller", "ticket_order_type_map"),
    ("history_controller", "ticket_rr_map"),
    ("history_controller", "ticket_source_map"),
    ("history_controller", "ticket_strategy_map"),
    ("notifications_controller", "ORB_CHART_CID"),
    ("schedule_controller", "describe_trading_clock"),
    ("schedule_controller", "parse_hm"),
    ("schedule_controller", "set_trading_clock_offset"),
    ("settings_controller", "get_app_config_async"),
    ("settings_controller", "get_expert_param_catalogue"),
    ("settings_controller", "live_log_lines"),
    ("settings_controller", "reset_all_expert_params"),
    ("settings_controller", "reset_expert_param"),
    ("settings_controller", "save_expert_params"),
    ("settings_controller", "switch_environment_db"),
    ("sync_controller", "is_centralized_remote_mode"),
    ("sync_controller", "is_remote_active"),
    ("sync_controller", "link_state"),
    ("sync_controller", "note_remote_setting"),
    ("sync_controller", "server_stop"),
    ("system_controller", "AUTOSTART_CHECK_INTERVAL_SECS"),
    ("system_controller", "autostart_disable"),
    ("system_controller", "autostart_enable"),
    ("system_controller", "autostart_is_armed"),
    ("system_controller", "autostart_is_installed"),
    ("system_controller", "autostart_is_supported"),
    ("system_controller", "local_today"),
    ("telegram_controller", "get_pending_unrecognised"),
    ("telegram_controller", "get_reader_status"),
    ("trading_controller", "get_channel_strategy_rec_map"),
    ("trading_controller", "get_channel_strategy_recs"),
}

# Known dead. Each is a controller operation nothing calls and nothing wants.
KNOWN_DEAD = {
    # Exported and tested, called by nothing. See the docstring.
    ("engines_controller", "engines_running"),
    # Named in its own module docstring as the one that returns a service
    # object -- and nothing asks it for one.
    ("sync_controller", "make_stats_facades"),
    ("sync_controller", "server_is_running"),
}


def _rel(path: str) -> str:
    return Path(path).relative_to(REPO).as_posix()


def _dead() -> set[tuple[str, str]]:
    out = set()
    for path in _CONTROLLERS:
        rel = _rel(path)
        mod = importlib.import_module(rel[:-3].replace("/", "."))
        for name in getattr(mod, "__all__", []):
            if name.startswith("_"):
                continue
            if not references_to(name, exclude=(rel,)):
                out.add((Path(rel).stem, name))
    return out


class TestEveryControllerOperationIsCalled:
    def test_no_new_routes_to_nowhere(self):
        unexpected = _dead() - KNOWN_DEAD - AWAITING_REACT_PORT

        assert not unexpected, (
            f"controller operations nothing calls: {sorted(unexpected)} — a "
            "controller names an operation for a router to use. Wire it up or "
            "delete it; do not add it to KNOWN_DEAD or AWAITING_REACT_PORT."
        )

    def test_neither_set_has_slack(self):
        """Both are exact. An entry that is no longer dead must be removed in
        the change that revives it, or the set stops describing anything."""
        assert _dead() == KNOWN_DEAD | AWAITING_REACT_PORT

    def test_the_two_sets_do_not_overlap(self):
        """'Nobody wants it' and 'its caller is not written yet' are different
        claims with different endings. A name in both is a name whose status
        nobody has decided."""
        assert not (KNOWN_DEAD & AWAITING_REACT_PORT)

    def test_the_port_debt_is_bounded_and_named(self):
        """The honest number, recorded so it can be watched shrinking.

        47 operations lost their caller on 2026-09-18 when eight NiceGUI tabs
        were deleted ahead of their React replacements. That is the cost of
        big-bang replace, stated rather than smoothed over. It may fall; it
        may not rise.
        """
        assert len(AWAITING_REACT_PORT) <= 47, (
            "the React port debt grew — a new tab deletion, or a controller "
            "operation added with no router to call it"
        )

    def test_the_layer_is_still_mostly_alive(self):
        """The number that makes this a gate rather than a wish. If exports
        ever drift far above callers for a reason that is NOT the port, this
        file is measuring the wrong thing and should be re-argued."""
        exported = 0
        for path in _CONTROLLERS:
            mod = importlib.import_module(_rel(path)[:-3].replace("/", "."))
            exported += sum(1 for n in getattr(mod, "__all__", [])
                            if not n.startswith("_"))

        assert exported > 200
        # Excluding the port debt, the layer is as tight as it ever was.
        assert len(_dead() - AWAITING_REACT_PORT) <= 5


class TestTheScannerCanSee:
    def test_a_live_operation_is_seen_as_live(self):
        assert references_to("get_risk_settings", exclude=())

    def test_a_name_nothing_mentions_is_seen_as_dead(self):
        assert not references_to("controller_operation_that_never_existed", exclude=())

    def test_tests_are_not_searched(self):
        """`engines_running` HAS a caller, in tests/. The scan looks only at
        backend/ and frontend/ on purpose, and this is the assertion that says
        so -- widen it to tests/ and this gate stops finding anything."""
        assert not references_to(
            "engines_running",
            exclude=("backend/src/controllers/engines_controller.py",))


@pytest.mark.parametrize("path", _CONTROLLERS)
def test_each_controller_declares_its_surface(path):
    """A controller with no `__all__` is invisible to this gate."""
    mod = importlib.import_module(_rel(path)[:-3].replace("/", "."))
    assert getattr(mod, "__all__", None), _rel(path)
