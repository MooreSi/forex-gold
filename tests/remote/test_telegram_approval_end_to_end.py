"""Approving from Telegram, all the way to a licensed client.

The owner's requirement, stated 2026-09-02: Telegram approval must work for
every new client, local or over the internet, and once approved it must update
both **the remote client** and **the Licence Manager in the admin console**.

The chain has five links and only the middle one was tested:

  1. the client registers, carrying its machine id
  2. the Telegram button resolves an 8-char prefix back to the real token
  3. `approve_registration` signs a licence FOR THAT MACHINE ID
  4. the console's Licence Manager is updated, and open consoles are pushed to
  5. the client reconnects and is handed the licence

Link 1 was broken: the MSG_REGISTER path never stored `machine_id`, so link 3
signed nothing and the client was approved with an empty key. The approval
"succeeded" and nothing happened -- which is exactly what the owner saw.

Nothing here touches Telegram or a socket. Every send is captured.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from backend.src.services.cluster.remote import server as rs
from backend.src.services.positions import _panel_registration as panel
from backend.src.services.cluster.remote.protocol import MSG_LICENCE

FINGERPRINT = "FOREX-349E9267-EBB61E27-539D9A41-3AAC333E"


class _Ws:
    def __init__(self):
        self.sent: list = []

    async def send(self, raw):
        self.sent.append(json.loads(raw))

    def types(self):
        return [m.get("type") for m in self.sent]


@pytest.fixture
def estate(monkeypatch):
    """A server with one machine pending approval, and a captured console."""
    inserted: list = []
    monkeypatch.setattr(rs, "_pending", {})
    monkeypatch.setattr(rs, "_allowed_tokens", {})
    monkeypatch.setattr(rs, "_revoked_tokens", set())
    monkeypatch.setattr(rs, "_connected", {})
    monkeypatch.setattr(rs, "_admin_clients", {})
    monkeypatch.setattr(rs, "_auth_failures", {})
    for name in ("_save_tokens", "_save_pending", "_save_revoked"):
        monkeypatch.setattr(rs, name, lambda: None)
    monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: f"SIG({mid}|{exp})")
    monkeypatch.setattr(rs, "_kg_insert_fn", lambda row: inserted.append(row))
    monkeypatch.setattr(rs, "_kg_get_all_fn", lambda: [])

    # The approval path fires three admin pushes through asyncio.create_task.
    # The sync tests here have no running loop, so create_task would raise
    # before reaching anything worth asserting -- while the async tests DO
    # have one and need the real scheduling. Delegate when a loop is running,
    # and close the coroutine otherwise so it is not reported as un-awaited.
    _real = asyncio.create_task

    def _task(coro, *a, **kw):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            coro.close()
            return None
        return _real(coro, *a, **kw)

    monkeypatch.setattr(asyncio, "create_task", _task)

    rs._pending["abcdef0123456789"] = {
        "hostname": "MacMini.localdomain", "platform": "darwin",
        "version": "1.2.3", "email": "simon@example.com",
        "nickname": "Simon's Mac", "ip": "192.168.0.53",
        "machine_id": FINGERPRINT, "ts": 0.0,
    }
    return inserted


class TestTheButtonReachesTheRightMachine:
    def test_an_eight_char_prefix_resolves_to_the_token(self, estate):
        """Telegram caps callback_data at 64 bytes and a token is 64 hex
        chars, so the button carries only the first eight."""
        assert panel._resolve_pending_token("abcdef01") == "abcdef0123456789"

    def test_an_unknown_prefix_resolves_to_nothing(self, estate):
        assert panel._resolve_pending_token("99999999") is None

    def test_a_stale_button_says_so_rather_than_failing_silently(self, estate):
        """Pressing Approve on a request already handled from the console.
        Silence would leave the admin unsure whether it worked."""
        screen = panel._approve_registration("99999999", "perp")

        assert "no longer pending" in screen.toast


class TestApprovingSignsForTheRightMachine:
    def test_the_licence_is_signed_for_the_machine_that_registered(self, estate):
        """Link 1 to link 3. This is the one that was broken: no machine_id in
        the pending entry meant nothing was signed."""
        panel._approve_registration("abcdef01", "perp")

        assert rs._allowed_tokens["abcdef0123456789"]["licence_key"] == \
            f"SIG({FINGERPRINT}|perpetual)"

    @pytest.mark.parametrize("code,label", [
        ("6m", "6 Months"), ("1y", "1 Year"), ("2y", "2 Years"),
        ("3y", "3 Years"), ("perp", "Perpetual"),
    ])
    def test_every_button_on_the_keyboard_works(self, estate, code, label):
        """All five are offered, so all five must approve."""
        panel._approve_registration("abcdef01", code)

        meta = rs._allowed_tokens["abcdef0123456789"]
        assert meta["subscription_type"] == label
        assert meta["licence_key"]

    def test_the_machine_leaves_the_pending_queue(self, estate):
        """Otherwise the console keeps offering it for approval and the
        registration notice fires again on the next reconnect."""
        panel._approve_registration("abcdef01", "perp")

        assert "abcdef0123456789" not in rs._pending

    def test_a_prior_revocation_is_lifted(self, estate):
        """Re-approving a machine that was revoked must actually let it back
        in, or the token is allowed and refused at the same time."""
        rs._revoked_tokens.add("abcdef0123456789")

        panel._approve_registration("abcdef01", "perp")

        assert "abcdef0123456789" not in rs._revoked_tokens

    def test_the_rate_limiter_is_cleared_for_that_ip(self, estate):
        """The newly approved client reconnects at once to collect its
        licence. The limiter must not block that."""
        rs._auth_failures["192.168.0.53"] = [1.0, 2.0, 3.0]

        panel._approve_registration("abcdef01", "perp")

        assert "192.168.0.53" not in rs._auth_failures


class TestTheConsoleLicenceManager:
    def test_the_approval_is_recorded_there(self, estate):
        """The owner's requirement in his own words: it must update the
        licence manager within the admin console."""
        inserted = estate

        panel._approve_registration("abcdef01", "perp")

        assert len(inserted) == 1

    def test_the_row_carries_what_the_manager_shows(self, estate):
        inserted = estate

        panel._approve_registration("abcdef01", "perp")

        row = inserted[0]
        assert row["registration_id"] == FINGERPRINT
        assert row["email"] == "simon@example.com"
        assert row["hostname"] == "MacMini.localdomain"
        assert row["licence_key"]
        assert row["expiry_date"] == "perpetual"

    def test_darwin_is_shown_as_macOS(self, estate):
        """The manager lists an OS, not a sys.platform string."""
        inserted = estate

        panel._approve_registration("abcdef01", "perp")

        assert inserted[0]["machine_model"] == "macOS"

    def test_it_is_not_recorded_twice(self, estate, monkeypatch):
        """Approving from Telegram after the console already issued it must
        not double the manager's list."""
        inserted = estate
        monkeypatch.setattr(
            rs, "_kg_get_all_fn",
            lambda: [{"licence_key": f"SIG({FINGERPRINT}|perpetual)"}])

        panel._approve_registration("abcdef01", "perp")

        assert inserted == []

    def test_a_row_without_a_machine_id_is_not_mirrored(self, estate):
        """An empty registration_id would put a row in the Licence Manager
        that identifies no machine and matches no install."""
        inserted = estate
        rs._allowed_tokens["tok"] = {"licence_key": "SIG(x)", "machine_id": ""}

        panel._record_licence_issued("tok")

        assert inserted == []

    def test_a_console_without_keygen_is_a_clean_no_op(self, estate, monkeypatch):
        """A node that is not running the admin console has no DB callbacks.
        It must approve fine and simply not mirror."""
        monkeypatch.setattr(rs, "_kg_insert_fn", None)

        screen = panel._approve_registration("abcdef01", "perp")

        assert "Approved" in screen.text


class TestTheClientGetsItsLicence:
    @pytest.mark.asyncio
    async def test_a_connected_client_is_sent_it_immediately(self, estate):
        ws = _Ws()
        rs._connected["abcdef0123456789"] = {"ws": ws, "info": {}}

        panel._approve_registration("abcdef01", "perp")
        import asyncio
        await asyncio.sleep(0)

        assert MSG_LICENCE in ws.types()
        assert ws.sent[0]["licence_key"] == f"SIG({FINGERPRINT}|perpetual)"

    @pytest.mark.asyncio
    async def test_a_disconnected_client_collects_it_on_reconnect(self, estate):
        """The real case. A machine on the activation screen registers and
        disconnects, so it is NOT connected when the admin approves -- it must
        be handed the licence when it comes back."""
        panel._approve_registration("abcdef01", "perp")
        ws = _Ws()

        await rs._send_licence(ws, "abcdef0123456789")

        assert MSG_LICENCE in ws.types()
        assert ws.sent[0]["machine_id"] == FINGERPRINT

    def test_the_hello_path_delivers_it_too(self):
        """Structural: the welcome sends MSG_LICENCE when the token carries
        one. That is what actually reaches a reconnecting client, and it is
        the step the owner is waiting on when he presses Approve."""
        import pathlib

        src = pathlib.Path(rs.__file__).read_text(encoding="utf-8")
        welcome = src[src.index("is_remote_admin=is_admin_machine_uuid"):]
        welcome = welcome[:welcome.index("MSG_VERSION_INFO")]

        assert 'tok_meta.get("licence_key")' in welcome
        assert "MSG_LICENCE" in welcome


class TestWhenSigningIsNotAvailable:
    def test_the_admin_is_warned_rather_than_left_guessing(self, estate,
                                                            monkeypatch):
        """Approved with no key is the state that looks like success and does
        nothing. It has to say so."""
        monkeypatch.setattr(rs, "_kg_sign_fn", None)

        screen = panel._approve_registration("abcdef01", "perp")

        assert "Licence key generation failed" in screen.text
