"""Strategy tunables and the saved templates built from them.

Split out of trading_controller for the same reason as schedule_controller:
that file crossed the 200-line controller ceiling.

set_strategy_params, reset_strategy_params and apply_template write values
that shape live sizing and exits. They are the calls the pages already made,
forwarded unchanged.
"""
from __future__ import annotations

from backend.src.services.risk import strategy_params as _sparams

__all__ = [
    "PARAM_SPECS", "PARAM_STRATEGIES", "STRATEGY_LABELS",
    "get_strategy_params", "set_strategy_params", "reset_strategy_params",
    "list_templates", "save_template", "delete_template", "apply_template",
]

PARAM_SPECS = _sparams.PARAM_SPECS
PARAM_STRATEGIES = _sparams.PARAM_STRATEGIES
STRATEGY_LABELS = _sparams.STRATEGY_LABELS


def get_strategy_params(*args, **kwargs):
    return _sparams.get_strategy_params(*args, **kwargs)


def set_strategy_params(*args, **kwargs):
    """Write a strategy's tunables. These shape live sizing and exits."""
    return _sparams.set_strategy_params(*args, **kwargs)


def reset_strategy_params(*args, **kwargs):
    """Put a strategy's tunables back to their defaults."""
    return _sparams.reset_strategy_params(*args, **kwargs)


def list_templates(*args, **kwargs):
    return _sparams.list_templates(*args, **kwargs)


def save_template(*args, **kwargs):
    return _sparams.save_template(*args, **kwargs)


def delete_template(*args, **kwargs):
    return _sparams.delete_template(*args, **kwargs)


def apply_template(*args, **kwargs):
    """Copy a saved template's values over a strategy's live parameters."""
    return _sparams.apply_template(*args, **kwargs)
