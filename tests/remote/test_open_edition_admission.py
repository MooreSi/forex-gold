"""An install of the open build joins the Remote Clients list by itself.

Owner, 2026-09-23: *someone downloaded the app from GitHub, installed it, and
it sent me a Telegram asking me to authorise the licence key -- when I asked
before to remove this requirement.* The licence gate itself was already off
(`config/edition.py`, 2026-09-22). What was left was the admin server: every
unknown client still filed a registration request, and every request still
arrived as Approve/Reject buttons. The machine that prompted it retried every
20 seconds for two and a half hours, 160 times, before it gave up.

What replaces the prompt, for a client that says it runs the open build:

* it is admitted straight into the Remote Clients list, where the console
  shows it like any other client;
* the owner gets one Telegram notification -- a new install -- with no buttons.

**It is not a licence.** Nothing is signed. An admitted record carries no
licence key and no machine id, so `resign_all_licences` -- which signs a key
for every approved client with a machine id, on every console start -- skips
it rather than quietly issuing one. A client that does NOT say it runs the
open build (every licensed build, and every build older than this change)
goes through the approval flow exactly as before. The field can only admit
what the owner has already made free; it cannot license anything.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.src.services.cluster.remote import _admission
from backend.src.services.cluster.remote import server as rs

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    d = tmp_path / "remote"
    d.mkdir()
    monkeypatch.setattr(rs, "_REMOTE_DIR", d)
    monkeypatch.setattr(rs, "_TOKENS_FILE", d / "allowed_tokens.json")
    monkeypatch.setattr(rs, "_PENDING_FILE", d / "pending_registrations.json")
    monkeypatch.setattr(rs, "_REVOKED_FILE", d / "revoked_tokens.json")
    monkeypatch.setattr(rs, "_allowed_tokens", {})
    monkeypatch.setattr(rs, "_pending", {})
    monkeypatch.setattr(rs, "_revoked_tokens", set())
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_admin_clients", {})
    monkeypatch.setattr(rs, "_auth_failures", {})
    monkeypatch.setattr(rs, "_kg_sign_fn", None)
    return d


@pytest.fixture
def telegram(monkeypatch):
    sent: list = []

    async def _send(text, reply_markup=None, **_kw):
        sent.append({"text": text, "reply_markup": reply_markup})
        return True

    from backend.src.services.telegram import alerts
    monkeypatch.setattr(alerts, "send_message", _send)

    async def _no_request_prompt(**kw):
        sent.append({"text": "REGISTRATION PROMPT", "reply_markup": "buttons"})
    monkeypatch.setattr(rs, "_notify_new_registration", _no_request_prompt)
    return sent


def _register(**over):
    msg = {"type": "register", "token": "tok-open-1", "hostname": "DESKTOP",
           "platform": "win32", "version": "0.5", "email": "", "nickname": "",
           "machine_id": "MACHINE-XYZ", "licence_required": False}
    msg.update(over)
    return msg


async def _settle():
    for _ in range(3):
        await asyncio.sleep(0)


# ── The open build is admitted ───────────────────────────────────────────────

class TestAnOpenInstall:
    async def test_it_appears_in_the_remote_clients_list(self, telegram):
        assert _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()

        clients = rs.get_all_clients()
        assert [c["token"] for c in clients] == ["tok-open-1"]
        assert clients[0]["hostname"] == "DESKTOP"
        assert clients[0]["platform"] == "win32"

    async def test_it_is_not_left_waiting_for_approval(self, telegram):
        rs._pending["tok-open-1"] = {"hostname": "DESKTOP"}

        _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()

        assert rs.get_pending_registrations() == []

    async def test_the_owner_is_told_once_with_no_buttons(self, telegram):
        _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()

        assert len(telegram) == 1
        assert telegram[0]["reply_markup"] is None
        assert "New install" in telegram[0]["text"]
        assert "DESKTOP" in telegram[0]["text"]
        assert "139.5.30.244" in telegram[0]["text"]

    async def test_it_survives_a_restart_of_the_server(self, telegram, isolated):
        _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()

        import json
        stored = json.loads((isolated / "allowed_tokens.json").read_text())
        assert "tok-open-1" in stored

    async def test_it_is_welcomed_on_its_next_connection(self, telegram):
        """The hello path admits anything in `_allowed_tokens`."""
        _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()

        assert "tok-open-1" in rs._allowed_tokens


# ── ...and it is not a licence ───────────────────────────────────────────────

class TestItIsNotALicence:
    async def test_no_key_is_signed(self, telegram, monkeypatch):
        signed: list = []
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: signed.append(mid) or "KEY")

        _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()

        assert signed == []
        assert not rs._allowed_tokens["tok-open-1"].get("licence_key")

    async def test_resigning_every_licence_does_not_issue_it_one(self, telegram, monkeypatch):
        """`resign_all_licences` runs on every console start and signs a key
        for any approved client with a machine id. An admitted record with
        one would be handed a real licence the next morning."""
        _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: "KEY")

        rs.resign_all_licences()

        assert not rs._allowed_tokens["tok-open-1"].get("licence_key")

    async def test_the_console_can_tell_it_from_a_paid_client(self, telegram):
        _admission.admit_open("tok-open-1", _register(), "139.5.30.244")
        await _settle()

        assert rs.get_all_clients()[0]["subscription_type"] == "Open source"


# ── Everything else goes through approval, as before ─────────────────────────

class TestEverythingElseIsUnchanged:
    @pytest.mark.parametrize("over", [
        {"licence_required": True},      # a licensed build
        {"licence_required": None},
        {"licence_required": "false"},   # not the boolean -- not consent
    ])
    async def test_a_build_that_does_not_say_it_is_open_is_not_admitted(self, telegram, over):
        assert not _admission.admit_open("tok-open-1", _register(**over), "1.2.3.4")
        await _settle()

        assert rs._allowed_tokens == {}
        assert telegram == []

    async def test_a_build_older_than_this_change_is_not_admitted(self, telegram):
        msg = _register()
        del msg["licence_required"]

        assert not _admission.admit_open("tok-open-1", msg, "1.2.3.4")

    async def test_a_revoked_client_is_not_let_back_in_by_itself(self, telegram):
        rs._revoked_tokens.add("tok-open-1")

        assert not _admission.admit_open("tok-open-1", _register(), "1.2.3.4")
        assert "tok-open-1" not in rs._allowed_tokens

    async def test_an_already_approved_client_is_left_alone(self, telegram):
        rs._allowed_tokens["tok-open-1"] = {"name": "Simon", "subscription_type": "1 Year",
                                            "licence_key": "PAID"}

        assert not _admission.admit_open("tok-open-1", _register(), "1.2.3.4")
        assert rs._allowed_tokens["tok-open-1"]["licence_key"] == "PAID"
        assert telegram == []

    async def test_a_blank_token_is_not_admitted(self, telegram):
        assert not _admission.admit_open("", _register(token=""), "1.2.3.4")


# ── The client says which build it is ────────────────────────────────────────

class TestTheClientSaysWhichBuildItIs:
    async def test_the_open_build_says_so(self, monkeypatch):
        from backend.src.services.cluster.remote import client as rc
        monkeypatch.delenv("FOREX_REQUIRE_LICENCE", raising=False)
        monkeypatch.setattr(rc, "get_or_create_token", lambda: "t")

        assert rc._build_register()["licence_required"] is False

    async def test_a_build_that_requires_a_licence_says_so(self, monkeypatch):
        from backend.src.services.cluster.remote import client as rc
        monkeypatch.setenv("FOREX_REQUIRE_LICENCE", "1")
        monkeypatch.setattr(rc, "get_or_create_token", lambda: "t")

        assert rc._build_register()["licence_required"] is True


# ── Both intake paths use it ─────────────────────────────────────────────────

class TestItIsWiredIn:
    """The server takes a registration in two places -- its own message, and a
    follow-up on the connection that was just told its token is unknown. The
    install that prompted this came in through the second."""

    def _src(self) -> str:
        import pathlib
        return pathlib.Path(rs.__file__).read_text(encoding="utf-8")

    async def test_the_register_message_is_checked_first(self):
        src = self._src()
        branch = src[src.index('if msg.get("type") == MSG_REGISTER:'):]
        branch = branch[:branch.index("await _close_ws(websocket)")]

        assert "_admission.admit_open(" in branch
        assert branch.index("_admission.admit_open(") < branch.index("_notify_new_registration")

    async def test_the_follow_up_is_checked_first(self):
        src = self._src()
        branch = src[src.index("if msg2.get(\"type\") == MSG_REGISTER:"):]
        branch = branch[:branch.index("except asyncio.TimeoutError")]

        assert "_admission.admit_open(" in branch
        assert branch.index("_admission.admit_open(") < branch.index("_notify_new_registration")
