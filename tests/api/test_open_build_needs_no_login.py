"""A dashboard that asks for no password must also not RENDER a login form.

Half of "no authentication" is the server: an app built without the gate
answers every request. The other half is the client, and it was the half that
would have been missed. `App.tsx` decides between the dashboard and the login
page from `/api/auth/session`, so a build with the gate uninstalled but the
session endpoint still reporting `auto_login: false` serves a login form that
nothing behind it is checking -- a password prompt with no password, which a
fresh clone cannot get past.

The gate itself is unchanged and still has its own file
(`tests/api/test_auth_gate.py`). Every behaviour there still holds for a build
that turns authentication back on.
"""
from __future__ import annotations

import pytest

from backend.src.api import auth as gate


@pytest.fixture
def ungated(make_client):
    """What `run.py` builds in the open-source build: no gate middleware."""
    return make_client(auth=False)


class TestTheServerHalf:
    def test_an_api_request_with_no_session_is_not_turned_away(self, ungated):
        """The same path the gated client is refused on
        (`test_auth_gate.py::test_an_unauthenticated_api_request_is_401...`).
        Asserted as "not 401" rather than "200" on purpose: whether this
        handler can reach a database is not what this file is about, and
        pinning 200 here would make it a second, worse test of the handler."""
        r = ungated.get("/api/trading/risk")
        assert r.status_code != 401

    def test_a_page_request_is_not_redirected_to_login(self, ungated):
        r = ungated.get("/trading", follow_redirects=False)
        assert r.status_code != 307
        assert r.headers.get("location") != "/login"


class TestTheClientHalf:
    def test_the_session_says_no_sign_in_is_needed(self, ungated, monkeypatch):
        """`auto_login` is what App.tsx reads to skip the login page. With
        authentication switched off at build level it must be true however the
        operator's own `auto_login_enabled` setting is left."""
        monkeypatch.setattr(gate, "auto_login_enabled", lambda: False)
        monkeypatch.setattr(gate, "authentication_required", lambda: False)
        assert ungated.get("/api/auth/session").json()["auto_login"] is True

    def test_a_build_that_requires_a_password_still_reports_the_setting(
        self, ungated, monkeypatch,
    ):
        """Negative control. Without it, a session endpoint hardcoded to true
        would pass the test above and would have silently disabled the login
        page for the licensed build too."""
        monkeypatch.setattr(gate, "authentication_required", lambda: True)
        monkeypatch.setattr(gate, "auto_login_enabled", lambda: False)
        assert ungated.get("/api/auth/session").json()["auto_login"] is False
        monkeypatch.setattr(gate, "auto_login_enabled", lambda: True)
        assert ungated.get("/api/auth/session").json()["auto_login"] is True


class TestTheGateStillWorksWhenItIsAskedFor:
    def test_installing_it_still_closes_the_door(self, make_client, monkeypatch):
        """Nothing was removed. A build that sets AUTHENTICATION_REQUIRED gets
        exactly the gate it had before."""
        monkeypatch.setattr(gate, "auto_login_enabled", lambda: False)
        assert make_client(auth=True).get("/api/trading/risk").status_code == 401
