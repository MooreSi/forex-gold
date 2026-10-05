"""Per-test isolation for the alert sender's module state.

`alerts._undelivered` remembers transport failures across calls, by design
(bugs/050). Between tests that memory is a leak: one test's simulated outage
would make the next test's first successful send carry a summary.
"""
import pytest

from backend.src.services.telegram import alerts


@pytest.fixture(autouse=True)
def _no_undelivered_alerts_carried_between_tests():
    alerts._undelivered.clear()
    yield
    alerts._undelivered.clear()
