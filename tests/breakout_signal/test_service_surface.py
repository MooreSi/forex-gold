"""Surface test for the task-030 decomposition -- catches a dropped or
mis-wired method on BreakoutEngine after splitting engine.py across
breakout_signal_service.py + the four mixin files. See
docs/todo/refactor/breakout-signal-migration/030-*.md.
"""
from backend.src.services.breakout_signal import breakout_signal_service as service
from backend.src.services.breakout_signal.breakout_signal_learn import _LearnMixin
from backend.src.services.breakout_signal.breakout_signal_live_execute import _LiveExecuteMixin
from backend.src.services.breakout_signal.breakout_signal_manage import _ManagementMixin
from backend.src.services.breakout_signal.breakout_signal_service import BreakoutEngine
from backend.src.services.breakout_signal.breakout_signal_velocity import _VelocityMixin

EXPECTED_METHODS = [
    "add_refresh_callback", "set_main_engine", "start", "stop",
    "_compute_cost_pts", "_close_and_learn", "_manage_triggered_signal", "_reconcile_live_pnl",
    "_velocity_loop", "_check_velocity_break",
    "_execute_live", "_maybe_stage_grid_template",
    "_run_batch_analysis",
    "_cycle_loop", "_run_cycle", "_process_candidate", "_outcome_loop", "_check_outcomes",
]


def test_every_expected_method_is_defined_and_callable():
    missing = [name for name in EXPECTED_METHODS if not hasattr(BreakoutEngine, name)]
    assert not missing, f"methods dropped in the split: {missing}"
    not_callable = [name for name in EXPECTED_METHODS if not callable(getattr(BreakoutEngine, name))]
    assert not not_callable, f"attributes present but not callable: {not_callable}"


def test_breakoutengine_composes_all_four_mixins():
    assert issubclass(BreakoutEngine, _ManagementMixin)
    assert issubclass(BreakoutEngine, _VelocityMixin)
    assert issubclass(BreakoutEngine, _LiveExecuteMixin)
    assert issubclass(BreakoutEngine, _LearnMixin)


def test_module_level_singleton_functions_present():
    assert callable(service.get_instance)
    assert callable(service.init)
    assert service.get_instance() is None


def test_mixin_methods_are_not_shadowed_by_duplicate_definitions():
    own   = set(vars(BreakoutEngine).keys())
    mgmt  = set(vars(_ManagementMixin).keys())
    vel   = set(vars(_VelocityMixin).keys())
    live  = set(vars(_LiveExecuteMixin).keys())
    learn = set(vars(_LearnMixin).keys())

    def _real(names):
        return {n for n in names if not (n.startswith("__") and n.endswith("__"))}

    groups = [_real(mgmt), _real(vel), _real(live), _real(learn)]
    overlaps = set()
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            overlaps |= groups[i] & groups[j]
    assert not overlaps, f"method name collision between mixins: {overlaps}"
