"""`run.main()` must refuse the second instance before it can do damage.

The order is the whole point. `_free_port()` kills whatever is listening, and
`_db_mod.init()` opens the database the other app is trading against, so both
must sit BEHIND the lock. A lock taken after either one would still have shot
the running app first and only then declined to replace it.

See `tests/utils/test_single_instance.py` for the lock itself.
"""
import pytest

import run
from backend.src.utils import single_instance


class TestTheBootPathHonoursTheLock:
    def test_it_returns_before_freeing_the_port(self, monkeypatch):
        """`_free_port` is a kill. If main() reaches it, the running app is
        already dead and the refusal came too late to matter."""
        calls = []
        monkeypatch.setattr(run, "_claim_single_instance", lambda: False)
        monkeypatch.setattr(run, "setup_logging", lambda: None)
        monkeypatch.setattr(run, "_ensure_data_dirs", lambda: None)
        monkeypatch.setattr(run, "_migrate_config_yaml", lambda: None)
        monkeypatch.setattr(run, "_free_port", lambda port: calls.append("free"))
        monkeypatch.setattr(run, "_start_mt5_bridge", lambda: calls.append("bridge"))

        run.main()

        assert calls == []

    def test_the_claim_is_made_before_the_config_is_even_read(self, monkeypatch):
        """Ordering, asserted from main() itself rather than from the source.

        Reading config is harmless; opening the database it names is not, and
        that follows immediately. Pinning the earlier of the two keeps the
        margin.
        """
        order = []
        monkeypatch.setattr(run, "setup_logging", lambda: None)
        monkeypatch.setattr(run, "_ensure_data_dirs", lambda: None)
        monkeypatch.setattr(run, "_migrate_config_yaml", lambda: None)
        monkeypatch.setattr(run, "_claim_single_instance",
                            lambda: order.append("lock") or False)

        import backend.src.config as cfg
        monkeypatch.setattr(cfg, "load", lambda: order.append("config") or {})

        run.main()

        assert order == ["lock"]


class TestTheClaimHelper:
    def test_it_succeeds_when_nothing_holds_the_lock(self, monkeypatch, tmp_path):
        import backend.src.config as cfg
        monkeypatch.setattr(cfg, "USER_DATA_DIR", tmp_path)
        try:
            assert run._claim_single_instance() is True
        finally:
            single_instance.release()

    def test_it_refuses_when_another_process_holds_it(self, monkeypatch, tmp_path):
        import backend.src.config as cfg
        monkeypatch.setattr(cfg, "USER_DATA_DIR", tmp_path)

        def _held(timeout=0.0):
            raise single_instance.AlreadyRunning(4321, "/Users/simon/Forex-Update", None)

        monkeypatch.setattr(single_instance, "acquire", _held)
        assert run._claim_single_instance() is False

    def test_it_fails_open_if_the_lock_cannot_be_taken_at_all(self, monkeypatch, caplog):
        """A read-only or missing data directory must not brick the app: that
        is a worse outcome than the overlap this guards against, and it is the
        behaviour every build before the lock already had. Loud, because
        silently losing the guard is how it stops being one.
        """
        def _broken(timeout=0.0):
            raise OSError("read-only file system")

        monkeypatch.setattr(single_instance, "acquire", _broken)
        with caplog.at_level("WARNING"):
            assert run._claim_single_instance() is True
        assert "read-only file system" in caplog.text

    def test_the_refusal_is_logged_with_the_holder(self, monkeypatch, caplog):
        def _held(timeout=0.0):
            raise single_instance.AlreadyRunning(4321, "/Users/simon/Forex-Update", None)

        monkeypatch.setattr(single_instance, "acquire", _held)
        with caplog.at_level("ERROR"):
            run._claim_single_instance()
        assert "4321" in caplog.text
        assert "Forex-Update" in caplog.text
