"""The composition root picks the fake reader in debug (stage2 phase5/020).

backend.src.app owns the choice — services stay swap-unaware, the import
contracts hold. Selection is a plain function so it is testable without a
full boot.

No test here can reach Telegram: readers are constructed, never started.
"""
from __future__ import annotations

from backend.src import app as backend_app
from backend.src.services.telegram.fake_reader import FakeTelegramReader
from backend.src.services.telegram.reader import TelegramReader


def test_reader_swap_in_debug():
    reader = backend_app._make_tg_reader({"debug_mode": True, "sessions_dir": "./data/test_sessions"})
    assert isinstance(reader, FakeTelegramReader)


def test_real_reader_when_debug_off():
    """Negative control: debug off builds the Telethon reader exactly as
    before."""
    reader = backend_app._make_tg_reader({"debug_mode": False, "sessions_dir": "./data/test_sessions"})
    assert isinstance(reader, TelegramReader)
    assert not isinstance(reader, FakeTelegramReader)
