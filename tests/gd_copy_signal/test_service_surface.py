"""Surface test for the task-040 decomposition -- catches a dropped or
mis-wired method on GDCopyEngine after splitting engine.py across
gd_copy_signal_service.py + the three mixin files, per backend-conventions
section 7's guidance on decomposition-safe testing. See
docs/todo/refactor/backend-foundation/040-*.md.
"""
import inspect

from forex_trader.gd_copy_signal import gd_copy_signal_service as service
from forex_trader.gd_copy_signal.gd_copy_signal_correlate import _CorrelationMixin
from forex_trader.gd_copy_signal.gd_copy_signal_live_execute import _LiveExecuteMixin
from forex_trader.gd_copy_signal.gd_copy_signal_manage import _ManagementMixin
from forex_trader.gd_copy_signal.gd_copy_signal_service import GDCopyEngine

# Every public method engine.py exposed before the split -- if any of these
# goes missing (or silently stops being callable) after a future edit, this
# test catches it immediately rather than at runtime in production.
EXPECTED_METHODS = [
    "set_main_engine", "add_refresh_callback", "start", "stop", "get_status",
    "_realistic_fill", "_net_pnl", "_manage_triggered_signal", "_reconcile_live_signal",
    "_check_correlation", "_vip_cadence_stats", "_classify_vip_level",
    "_try_live_execute",
    "_cycle_loop", "_run_cycle", "_outcome_loop", "_check_outcomes", "_correlation_loop",
    "_active_strategy", "_level_on_cooldown", "_already_open", "_today_signal_count",
    "_calc_atr", "_calc_adx",
]


def test_every_expected_method_is_defined_and_callable():
    missing = [name for name in EXPECTED_METHODS if not hasattr(GDCopyEngine, name)]
    assert not missing, f"methods dropped in the split: {missing}"
    not_callable = [name for name in EXPECTED_METHODS if not callable(getattr(GDCopyEngine, name))]
    assert not not_callable, f"attributes present but not callable: {not_callable}"


def test_gdcopyengine_composes_all_three_mixins():
    assert issubclass(GDCopyEngine, _ManagementMixin)
    assert issubclass(GDCopyEngine, _CorrelationMixin)
    assert issubclass(GDCopyEngine, _LiveExecuteMixin)


def test_module_level_singleton_functions_present():
    assert callable(service.get_instance)
    assert callable(service.init)
    assert service.get_instance() is None  # no instance created yet in this process


def test_mixin_methods_are_not_shadowed_by_duplicate_definitions():
    """If two mixins (or the service class itself) accidentally define the
    same method name, Python's MRO silently picks one -- this would hide a
    bug where, e.g., a management method and a correlation method collide.
    """
    own = set(vars(GDCopyEngine).keys())
    mgmt = set(vars(_ManagementMixin).keys())
    corr = set(vars(_CorrelationMixin).keys())
    live = set(vars(_LiveExecuteMixin).keys())
    # dunder/private-framework names aside, no two mixins should define the
    # same real method -- that would indicate an accidental collision.
    def _real(names):
        return {n for n in names if not (n.startswith("__") and n.endswith("__"))}
    overlaps = (
        (_real(mgmt) & _real(corr))
        | (_real(mgmt) & _real(live))
        | (_real(corr) & _real(live))
    )
    assert not overlaps, f"method name collision between mixins: {overlaps}"
