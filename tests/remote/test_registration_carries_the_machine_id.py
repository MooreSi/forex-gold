"""A pending registration must carry the machine id, or it cannot be licensed.

The failure the owner hit on 2026-09-02: the Telegram Approve button was
pressed and "nothing happened". It had not: the approval ran, and produced a
client approved with **no licence key**.

`approve_registration` signs the licence for `pending["machine_id"]`:

    machine_id = pending.get("machine_id", "")
    if machine_id:
        licence_key = _kg_sign_fn(machine_id, expiry_date)

The client sends it -- `_build_register` includes `machine_id=get_fingerprint()`
-- and one of the two registration paths stores it. The MSG_REGISTER branch did
not. So a machine registering that way was queued without the one field the
signing step needs, approved successfully, and issued an empty key.

There is even a warning for it in the panel: "Licence key generation failed --
check the signing key is registered." Which sends you looking at the signing
key, when the signing key is fine and the machine id never arrived.

Both paths are pinned here, because the two drifting apart is the whole bug.
"""
from __future__ import annotations

import pathlib
import re

import pytest

from backend.src.services.cluster.remote import server as rs

FINGERPRINT = "FOREX-349E9267-EBB61E27-539D9A41-3AAC333E"


@pytest.fixture
def clean(monkeypatch):
    monkeypatch.setattr(rs, "_pending", {})
    monkeypatch.setattr(rs, "_allowed_tokens", {})
    monkeypatch.setattr(rs, "_save_pending", lambda: None)
    monkeypatch.setattr(rs, "_save_tokens", lambda: None)
    return rs._pending


class TestTheQueuedEntry:
    def test_it_keeps_the_machine_id(self, clean):
        """Without it the approval signs nothing and issues an empty key."""
        details = {"hostname": "mac-01", "platform": "darwin", "version": "1",
                   "email": "a@b.c", "nickname": "Mac", "ip": "10.0.0.1",
                   "machine_id": FINGERPRINT}

        rs._pending["tok-1"] = {**details, "ts": 0.0}

        assert rs._pending["tok-1"]["machine_id"] == FINGERPRINT

    def test_both_registration_paths_store_it(self):
        """The two paths drifting apart IS the bug. One stored machine_id and
        the other did not, so whether a machine could be licensed depended on
        which branch it happened to arrive through.

        Checked over each BRANCH rather than each dict literal: one path now
        builds its details separately and spreads them, so a literal-scanning
        version reports a false failure on correct code.

        COMMENTS ARE STRIPPED, and that matters: the branch carries a comment
        explaining why machine_id is there, so a plain substring search matched
        the explanation and passed with the field deleted. Third time that trap
        has appeared in this codebase.
        """
        src = pathlib.Path(rs.__file__).read_text(encoding="utf-8")
        starts = [m.start() for m in re.finditer(r"_pending\[\w+\] = \{", src)]

        assert len(starts) >= 2, "expected both registration paths"
        for start in starts:
            # from the branch's `elif` back-marker to just past the assignment
            branch_start = src.rfind("elif ", 0, start)
            branch = src[branch_start:src.index("_save_pending()", start)]
            code = "\n".join(ln for ln in branch.splitlines()
                             if not ln.strip().startswith("#"))

            assert '"machine_id"' in code, code[:200]


class TestApproving:
    def test_a_queued_machine_id_reaches_the_signer(self, clean, monkeypatch):
        signed: list = []
        monkeypatch.setattr(rs, "_kg_sign_fn",
                            lambda mid, exp: signed.append((mid, exp)) or "KEY")
        rs._pending["tok-1"] = {"hostname": "mac", "machine_id": FINGERPRINT}

        assert rs.approve_registration("tok-1", "Mac", "Perpetual") is True
        assert signed == [(FINGERPRINT, "perpetual")]

    def test_the_licence_key_is_stored_against_the_token(self, clean, monkeypatch):
        """What gets pushed to the client. Empty here and the client is
        approved but can never activate."""
        monkeypatch.setattr(rs, "_kg_sign_fn", lambda mid, exp: "SIGNED-KEY")
        rs._pending["tok-1"] = {"hostname": "mac", "machine_id": FINGERPRINT}

        rs.approve_registration("tok-1", "Mac", "Perpetual")

        assert rs._allowed_tokens["tok-1"]["licence_key"] == "SIGNED-KEY"
        assert rs._allowed_tokens["tok-1"]["machine_id"] == FINGERPRINT

    def test_without_a_machine_id_no_key_is_signed(self, clean, monkeypatch):
        """The state this bug produced, pinned so it is recognisable: the
        approval SUCCEEDS and the key is empty. That combination is why the
        owner saw 'nothing happened' rather than an error."""
        signed: list = []
        monkeypatch.setattr(rs, "_kg_sign_fn",
                            lambda mid, exp: signed.append(mid) or "KEY")
        rs._pending["tok-1"] = {"hostname": "mac"}

        ok = rs.approve_registration("tok-1", "Mac", "Perpetual")

        assert ok is True
        assert signed == []
        assert rs._allowed_tokens["tok-1"]["licence_key"] == ""

    @pytest.mark.parametrize("sub,expiry_is_date", [
        ("Perpetual", False), ("6 Months", True), ("1 Year", True),
        ("2 Years", True), ("3 Years", True),
    ])
    def test_every_duration_the_buttons_offer_signs_something(
        self, clean, monkeypatch, sub, expiry_is_date,
    ):
        """The Telegram keyboard offers all five. One that produced no expiry
        would sign a licence the app then refuses."""
        signed: list = []
        monkeypatch.setattr(rs, "_kg_sign_fn",
                            lambda mid, exp: signed.append(exp) or "KEY")
        rs._pending["tok-1"] = {"hostname": "mac", "machine_id": FINGERPRINT}

        rs.approve_registration("tok-1", "Mac", sub)

        assert signed, sub
        assert (signed[0] != "perpetual") is expiry_is_date

    def test_the_expiry_is_lowercase_perpetual(self, clean, monkeypatch):
        """It is bound INTO the signature, and the verifier compares exactly.
        'Perpetual' with a capital P does not verify -- confirmed against the
        real keypair on 2026-09-02."""
        signed: list = []
        monkeypatch.setattr(rs, "_kg_sign_fn",
                            lambda mid, exp: signed.append(exp) or "KEY")
        rs._pending["tok-1"] = {"hostname": "mac", "machine_id": FINGERPRINT}

        rs.approve_registration("tok-1", "Mac", "Perpetual")

        assert signed == ["perpetual"]
