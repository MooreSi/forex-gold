"""Connections: the two outbound channels, and the buttons that prove them.

**Nothing in this file sends anything.** `send_email` and `send_message` are
replaced with recorders; the assertions are about what would have been sent and
to whom, which is the part a test can own. Whether Resend accepts the key is not
a question a test suite can answer, and pretending otherwise is what the test
sends exist for.

Two behaviours here are worth more than the routing:

  * **What is not given is kept.** The dashboard saves one field as the
    operator leaves it, so a Telegram write that named only the chat ID used to
    switch alerts off and throw the bot token away. Three tests hold that shut.
  * **A failure says something useful.** A test send that fails returns the
    translated SMTP message, not "Failed" — the Outlook basic-auth case has a
    five-click fix that nobody guesses from `535 5.7.139`.
"""
from __future__ import annotations

import pytest

from backend.src.api.routers import notifications as notif_router
from backend.src.services.notifications import config as notify_config


@pytest.fixture
def store(monkeypatch):
    """The two rows and the config file, in memory, with every send recorded."""
    state = {
        "email": {
            "smtp_host": "smtp.example.com", "smtp_password": "hunter2",
            "to_addr": "me@example.com", "send_provider": "resend",
        },
        "telegram": {"bot_token_enc": "123:ABC", "chat_id": "-100", "enabled": 1},
        "yaml": {"telegram_api_id": "77", "telegram_api_hash": "beef",
                 "telegram_phone": "+44", "claude_api_key": "sk-secret"},
        "sent": [],
        "alerts": [],
        "orb": {"symbol": "XAUUSD", "high": 4000.0},
        "send_ok": True,
        "send_err": "",
    }

    async def _send_email(subject, html, cfg, **kwargs):
        state["sent"].append({"subject": subject, "html": html, "cfg": cfg, **kwargs})
        return state["send_ok"], state["send_err"]

    async def _send_message(text, **kwargs):
        state["alerts"].append((text, kwargs))
        return state["send_ok"]

    async def _orb():
        return state["orb"]

    monkeypatch.setattr(notif_router.settings_ctl, "get_email_config",
                        lambda: dict(state["email"]))
    monkeypatch.setattr(notif_router.settings_ctl, "save_email_config",
                        lambda v: state["email"].update(v))
    monkeypatch.setattr(notif_router.settings_ctl, "get_telegram_config",
                        lambda: dict(state["telegram"]))
    monkeypatch.setattr(notif_router.settings_ctl, "load_config",
                        lambda: dict(state["yaml"]))
    monkeypatch.setattr(notif_router.settings_ctl, "save_config",
                        lambda v: state["yaml"].update(v))
    monkeypatch.setattr(notif_router.notify_ctl, "send_email", _send_email)
    monkeypatch.setattr(notif_router.notify_ctl, "build_orb_report", _orb)
    monkeypatch.setattr(notif_router.notify_ctl, "build_orb_chart_image",
                        lambda r: b"PNG")
    monkeypatch.setattr(notif_router.notify_ctl, "build_orb_html",
                        lambda r, d, has_chart=False: f"<html>{d}</html>")
    monkeypatch.setattr(notif_router.telegram_ctl, "send_message", _send_message)

    # The real service, over the in-memory row: the keep-what-is-not-given rule
    # lives there and a recorder in its place would assert nothing.
    monkeypatch.setattr(notify_config, "_tg_repo", _FakeTelegramRepo(state))
    monkeypatch.setattr(notif_router.settings_ctl, "save_telegram_config",
                        notify_config.save_telegram)
    monkeypatch.setattr(notify_config, "get_telegram", lambda: dict(state["telegram"]))
    return state


class _FakeTelegramRepo:
    def __init__(self, state):
        self._state = state

    def save_telegram_config(self, bot_token, chat_id, enabled):
        self._state["telegram"] = {
            "bot_token_enc": bot_token, "chat_id": chat_id, "enabled": int(enabled),
        }


def _message(res) -> str:
    """The refusal text, from the one error shape the whole API uses.

    `{"error": {"kind", "message", "ref"}}` — not FastAPI's `detail`, because a
    refusal and an unexpected failure have to be told apart by the client.
    """
    return res.json()["error"]["message"]


# ── Reading, without leaking ─────────────────────────────────────────────────

def test_the_bot_token_never_reaches_the_browser(make_client, store):
    body = make_client().get("/api/settings/telegram").text

    assert "123:ABC" not in body


def test_a_configured_bot_is_reported_as_set(make_client, store):
    body = make_client().get("/api/settings/telegram").json()

    assert body["bot_token_enc_set"] is True
    assert body["chat_id"] == "-100"


def test_the_reader_answers_with_its_three_keys_and_no_others(make_client, store):
    """`config.yaml` also holds the AI keys. Naming what is wanted is safer
    than redacting whatever happens to be in the file."""
    body = make_client().get("/api/settings/telegram-reader").json()

    assert body["telegram_api_id"] == "77"
    assert body["telegram_api_hash_set"] is True
    assert "claude_api_key" not in body
    assert "claude_api_key_set" not in body


# ── What is not given is kept ────────────────────────────────────────────────

class TestAPartialWriteKeepsTheRest:
    """The dashboard saves one field at a time. Each of these was a real loss
    before the service learnt the rule."""

    def test_changing_the_chat_id_does_not_switch_alerts_off(self, make_client, store):
        make_client().put("/api/settings/telegram", json={"chat_id": "-200"})

        assert store["telegram"]["enabled"] == 1
        assert store["telegram"]["chat_id"] == "-200"

    def test_changing_the_chat_id_does_not_erase_the_token(self, make_client, store):
        """The token field is empty on screen whether or not one is stored, so
        passing it through would wipe a working bot on every edit."""
        make_client().put("/api/settings/telegram", json={"chat_id": "-200"})

        assert store["telegram"]["bot_token_enc"] == "123:ABC"

    def test_switching_alerts_off_keeps_the_token_and_the_chat(self, make_client, store):
        make_client().put("/api/settings/telegram", json={"enabled": False})

        assert store["telegram"] == {
            "bot_token_enc": "123:ABC", "chat_id": "-100", "enabled": 0,
        }

    def test_a_token_that_is_actually_given_replaces_the_stored_one(
        self, make_client, store,
    ):
        """Negative control. A rule that kept the old token unconditionally
        would pass all three tests above and make the field useless."""
        make_client().put("/api/settings/telegram", json={"bot_token": "999:ZZZ"})

        assert store["telegram"]["bot_token_enc"] == "999:ZZZ"


def test_the_reader_write_ignores_a_key_it_was_not_expecting(make_client, store):
    """`save_to_yaml` merges, so an unrecognised key would be written and kept
    for ever."""
    make_client().put("/api/settings/telegram-reader",
                      json={"telegram_phone": "+99", "licence_key": "stolen"})

    assert store["yaml"]["telegram_phone"] == "+99"
    assert "licence_key" not in store["yaml"]


# ── Proving a connection works ───────────────────────────────────────────────

class TestTheTestSends:
    def test_a_test_email_goes_to_the_configured_address(self, make_client, store):
        res = make_client().post("/api/notifications/test-email", json={})

        assert res.status_code == 200
        assert res.json()["to"] == "me@example.com"
        assert len(store["sent"]) == 1

    def test_it_can_prove_the_provider_the_operator_just_picked(
        self, make_client, store,
    ):
        """Saved config still says resend; the operator is looking at gmail.
        Testing the saved one proves the wrong thing."""
        make_client().post("/api/notifications/test-email", json={"provider": "gmail"})

        assert store["sent"][0]["cfg"]["send_provider"] == "gmail"

    def test_the_override_is_not_written_to_the_stored_config(self, make_client, store):
        """A test is a test. Picking a provider in the dropdown and pressing
        Test must not silently change what the scheduler uses."""
        make_client().post("/api/notifications/test-email", json={"provider": "gmail"})

        assert store["email"]["send_provider"] == "resend"

    def test_with_no_recipient_it_refuses_rather_than_sending_nowhere(
        self, make_client, store,
    ):
        store["email"]["to_addr"] = ""

        res = make_client().post("/api/notifications/test-email", json={})

        assert res.status_code == 409
        assert "Send reports to" in _message(res)
        assert store["sent"] == []

    def test_a_failure_carries_the_translated_message(self, make_client, store):
        """`535 5.7.139` has a five-click fix nobody guesses. The translation
        is the whole value of the button."""
        store["send_ok"] = False
        store["send_err"] = "535 5.7.139 Authentication unsuccessful"

        detail = _message(make_client().post(
            "/api/notifications/test-email", json={}))

        assert "Authenticated SMTP" in detail
        assert "5.7.139" in detail, "the original must still be reportable"

    def test_the_orb_test_sends_the_real_report_with_its_chart(
        self, make_client, store,
    ):
        res = make_client().post("/api/notifications/test-orb-report")

        assert res.status_code == 200
        assert store["sent"][0]["image_bytes"] == b"PNG"
        assert store["sent"][0]["image_cid"]

    def test_a_report_that_cannot_be_built_is_not_sent_empty(self, make_client, store):
        """"No bridge, no candles" and "sent you a blank page" look identical
        in an inbox, and only one tells the operator to check MT5."""
        store["orb"] = None

        res = make_client().post("/api/notifications/test-orb-report")

        assert res.status_code == 409
        assert "MT5 bridge" in _message(res)
        assert store["sent"] == []

    def test_the_telegram_test_uses_the_saved_settings(self, make_client, store):
        res = make_client().post("/api/notifications/test-telegram")

        assert res.status_code == 200
        assert "Test Alert" in store["alerts"][0][0]

    def test_a_telegram_refusal_says_what_to_check(self, make_client, store):
        store["send_ok"] = False

        res = make_client().post("/api/notifications/test-telegram")

        assert res.status_code == 409
        assert "bot token" in _message(res)


class TestSendingIsNeverAGet:
    """A GET that sends is a GET a browser prefetch, a link checker or a
    refresh can fire."""

    @pytest.mark.parametrize("path", [
        "/api/notifications/test-email",
        "/api/notifications/test-orb-report",
        "/api/notifications/test-telegram",
    ])
    def test_a_get_is_rejected_and_sends_nothing(self, path, make_client, store):
        res = make_client().get(path)

        assert res.status_code == 405
        assert store["sent"] == []
        assert store["alerts"] == []
