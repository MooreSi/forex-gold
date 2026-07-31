"""Surface test for the task-030 decomposition -- catches a dropped or
mis-wired method on TestSignalEngine after splitting engine.py across
test_signal_service.py + the four mixin files. See
docs/todo/refactor/test-signal-migration/030-*.md.
"""
from backend.src.services.test_signal import test_signal_service as service
from backend.src.services.test_signal.test_signal_generate import _GenerateMixin
from backend.src.services.test_signal.test_signal_learn import _LearnMixin
from backend.src.services.test_signal.test_signal_live_execute import _LiveExecuteMixin
from backend.src.services.test_signal.test_signal_manage import _ManagementMixin
from backend.src.services.test_signal.test_signal_service import TestSignalEngine
from backend.src.services.test_signal.test_signal_velocity import _VelocityMixin

EXPECTED_METHODS = [
    "start", "stop", "set_main_engine", "add_refresh_callback",
    "_watchdog_loop",
    "_reconcile_live_pnl", "_generate_learning_note", "_close_signal", "_manage_triggered_signal",
    "_velocity_loop",
    "_execute_live",
    "_run_batch_analysis",
    "_cycle_loop", "_run_cycle", "_outcome_loop", "_check_outcomes",
]

EXPECTED_PROPERTIES = ["is_running", "status", "status_detail", "last_cycle_at"]


def test_every_expected_method_is_defined_and_callable():
    missing = [name for name in EXPECTED_METHODS if not hasattr(TestSignalEngine, name)]
    assert not missing, f"methods dropped in the split: {missing}"
    not_callable = [name for name in EXPECTED_METHODS if not callable(getattr(TestSignalEngine, name))]
    assert not not_callable, f"attributes present but not callable: {not_callable}"


def test_every_expected_property_is_defined():
    missing = [name for name in EXPECTED_PROPERTIES if not isinstance(getattr(TestSignalEngine, name, None), property)]
    assert not missing, f"properties dropped in the split: {missing}"


def test_testsignalengine_composes_all_five_mixins():
    assert issubclass(TestSignalEngine, _GenerateMixin)
    assert issubclass(TestSignalEngine, _ManagementMixin)
    assert issubclass(TestSignalEngine, _VelocityMixin)
    assert issubclass(TestSignalEngine, _LiveExecuteMixin)
    assert issubclass(TestSignalEngine, _LearnMixin)


def test_module_level_singleton_functions_present():
    assert callable(service.get_instance)
    assert callable(service.init)
    assert service.get_instance() is None


def test_mixin_methods_are_not_shadowed_by_duplicate_definitions():
    gen   = set(vars(_GenerateMixin).keys())
    mgmt  = set(vars(_ManagementMixin).keys())
    vel   = set(vars(_VelocityMixin).keys())
    live  = set(vars(_LiveExecuteMixin).keys())
    learn = set(vars(_LearnMixin).keys())

    def _real(names):
        return {n for n in names if not (n.startswith("__") and n.endswith("__"))}

    groups = [_real(gen), _real(mgmt), _real(vel), _real(live), _real(learn)]
    overlaps = set()
    for i in range(len(groups)):
        for j in range(i + 1, len(groups)):
            overlaps |= groups[i] & groups[j]
    assert not overlaps, f"method name collision between mixins: {overlaps}"
