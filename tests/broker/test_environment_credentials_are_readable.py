"""A saved password that cannot be DECRYPTED is not a missing password.

Found on the running app, 2026-09-19: the DEMO badge in the header refused to
open, with the tooltip "No live account is configured. Add it under
Settings > MT5." Both accounts were fully configured -- login, password and
server all stored, and the app was trading on the demo one at the time.

What had actually happened is that the app was running under an interpreter
with no `keyring` module, so `config.secrets` fell back to a key file, the key
did not match, and `decrypt()` returned "" for every password. `describe()`
folded that into `configured: false`, and the UI turned it into "go and enter
the credentials" -- which sends the operator to re-type credentials that are
already there, and would not have fixed it.

Three states, not two:

    configured    saved and readable -- the switch can run
    unreadable    saved but the key cannot decrypt them
    missing       nothing saved

Only the third is the operator's to fix by typing.
"""
from __future__ import annotations

import pytest

from backend.src.services.broker import environment as env


@pytest.fixture
def creds(monkeypatch):
    """The stored row, with the passwords already through decrypt()."""
    state = {
        "login": "26004592", "password_enc": "hunter2",
        "server": "VantageMarkets-Demo",
        "live_login": "29377272", "live_password_enc": "hunter3",
        "live_server": "VantageMarkets-Live 6",
    }
    monkeypatch.setattr(env._creds, "get_mt5_credentials", lambda: dict(state))
    monkeypatch.setattr(env, "current", lambda: "demo")
    return state


class TestWhenEverythingIsReadable:

    def test_both_environments_are_configured(self, creds):
        out = env.describe()["environments"]

        assert out["demo"]["configured"] is True
        assert out["live"]["configured"] is True

    def test_neither_is_flagged_unreadable(self, creds):
        out = env.describe()["environments"]

        assert out["demo"]["unreadable"] is False
        assert out["live"]["unreadable"] is False


class TestWhenTheKeyCannotDecryptThem:

    def test_it_is_not_configured(self, creds):
        # It genuinely cannot be switched to: `switch()` needs the password.
        creds["live_password_enc"] = ""

        assert env.describe()["environments"]["live"]["configured"] is False

    def test_it_says_the_password_is_unreadable_rather_than_absent(self, creds):
        # The distinction the whole file exists for. "Add it under Settings >
        # MT5" is wrong advice here and would not fix anything.
        creds["live_password_enc"] = ""

        assert env.describe()["environments"]["live"]["unreadable"] is True

    def test_the_login_and_server_still_come_back(self, creds):
        # They are what tell the operator this is a saved account rather than
        # an empty slot.
        creds["live_password_enc"] = ""
        live = env.describe()["environments"]["live"]

        assert live["login"] == "29377272"
        assert live["server"] == "VantageMarkets-Live 6"

    def test_one_unreadable_account_does_not_condemn_the_other(self, creds):
        creds["live_password_enc"] = ""
        out = env.describe()["environments"]

        assert out["demo"]["configured"] is True
        assert out["demo"]["unreadable"] is False


class TestWhenNothingIsSaved:

    def test_an_empty_slot_is_not_unreadable(self, creds):
        # Nothing was stored, so nothing failed to decrypt. This IS the case
        # where typing the credentials fixes it.
        creds["live_login"] = ""
        creds["live_server"] = ""
        creds["live_password_enc"] = ""
        live = env.describe()["environments"]["live"]

        assert live["configured"] is False
        assert live["unreadable"] is False

    def test_a_login_with_no_server_is_incomplete_not_unreadable(self, creds):
        creds["live_server"] = ""
        live = env.describe()["environments"]["live"]

        assert live["configured"] is False
        assert live["unreadable"] is False


class TestTheSwitchStillRefuses:

    def test_it_will_not_switch_to_an_account_it_cannot_read(self, creds, monkeypatch):
        # The refusal is correct and stays. Only the EXPLANATION was wrong.
        creds["live_password_enc"] = ""

        with pytest.raises(ValueError) as caught:
            env.switch("live")

        assert "Live" in str(caught.value)
