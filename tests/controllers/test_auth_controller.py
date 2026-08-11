"""auth_controller forwards the first-run setup API (review 2026-08-11, C2).

The frontend may only reach auth through this controller (layer rules), so
the setup flow the login gate needs must be exposed here: `needs_setup` and
`create_initial_password`, thin forwarders to services/auth/dashboard_auth —
same shape as the existing verify/is_set/set_password forwarders.

No test in this file can reach a broker or the network.
"""
from __future__ import annotations

from unittest.mock import patch

from backend.src.controllers import auth_controller
from backend.src.services.auth import dashboard_auth as _svc


def test_needs_setup_forwards_to_the_service():
    with patch.object(_svc, "needs_setup", return_value=True, create=True) as fwd:
        assert auth_controller.needs_setup() is True
    assert fwd.call_count == 1


def test_create_initial_password_forwards_the_password():
    with patch.object(_svc, "create_initial_password", return_value=True,
                      create=True) as fwd:
        assert auth_controller.create_initial_password("s3cret-pass") is True
    assert fwd.call_args.args == ("s3cret-pass",)
