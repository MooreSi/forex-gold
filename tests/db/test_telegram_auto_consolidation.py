""""Telegram Auto (X)" and "X" are one channel.

Owner, 2026-09-03: "telegram auto and telegram are the same thing so
consolidate them".

They were two everywhere the scorecard touched, and one everywhere else --
which is how a single channel ended up with two sets of stats, two lot
multipliers and two paused flags:

    Gold Diggers VIP                    1.3  paused=1  188 samples
    Telegram Auto (Gold Diggers VIP)    1.3  paused=0   17 samples

`_canonical()` already strips the wrapper, and the strategy lookups all use
it. `_normalise_tg_source` -- which is what the SCORECARD groups by -- only
mapped numeric group IDs to names and left the wrapper alone. So the scorecard
aggregated the two routes separately and `recompute_channel_performance`
wrote a row for each.

Four readers then queried `WHERE source=?` with the raw name, finding whichever
row matched literally. `get_channel_lot_mult` is the one that matters: it
returns the multiplier AND the paused flag used by resolution.py, so the two
routes could size and pause differently for the same channel.

Owner's decision on the merge conflict (2026-09-03): the merged channel is
NOT paused. The Telegram Auto route has been trading all week -- 69 trades in
30 days -- and honouring the bare channel's manual pause would have stopped it
silently.
"""
from __future__ import annotations

import pytest

from backend.src.services.channels import repo as ch
from backend.src.services.channels import scorecard_repo as sv


class TestTheWrapperIsStripped:
    @pytest.mark.parametrize("raw,expected", [
        ("Telegram Auto (Gold Diggers VIP)", "Gold Diggers VIP"),
        ("Telegram Auto (GOLD DIGGERS INSTITUTIONAL)", "GOLD DIGGERS INSTITUTIONAL"),
        ("telegram auto (Gold Diggers VIP)", "Gold Diggers VIP"),
    ])
    def test_the_scorecard_groups_them_together(self, raw, expected):
        """This is the one that created the duplicate rows."""
        assert ch._normalise_tg_source(raw) == expected

    def test_a_bare_channel_is_unchanged(self):
        assert ch._normalise_tg_source("Gold Diggers VIP") == "Gold Diggers VIP"

    def test_a_name_that_merely_contains_the_words_is_untouched(self):
        """Only the exact wrapper, or a channel legitimately called something
        like this loses its identity."""
        assert ch._normalise_tg_source("Telegram Automation Desk") == \
            "Telegram Automation Desk"

    def test_an_unclosed_wrapper_is_left_alone(self):
        assert ch._normalise_tg_source("Telegram Auto (Gold") == \
            "Telegram Auto (Gold"

    def test_empty_stays_empty(self):
        assert ch._normalise_tg_source("") == ""


class TestEveryReaderResolvesTheSameRow:
    """A lookup that does not canonicalise finds a different row for the same
    channel -- which is exactly how the two routes diverged."""

    @pytest.mark.parametrize("module,fn_name", [
        (ch, "get_channel_lot_mult"),
        (ch, "set_channel_paused"),
        (ch, "recompute_channel_performance"),
        # Moved to scorecard_repo 2026-09-03 to keep repo.py under its line
        # ceiling. Named by its real home so this test cannot pass by finding
        # a stale copy.
        (sv, "get_channel_performance_map"),
    ])
    def test_it_canonicalises(self, module, fn_name):
        import inspect

        src = inspect.getsource(getattr(module, fn_name))
        code = "\n".join(ln for ln in src.splitlines()
                         if not ln.strip().startswith("#"))

        assert "_canonical(" in code or "_normalise_tg_source(" in code, fn_name


class TestTheMultiplierLookup:
    """The money-path one: it returns the multiplier AND the paused flag that
    resolution.py uses to size a trade and to refuse one."""

    @pytest.fixture
    def rows(self, monkeypatch):
        store = {"Gold Diggers VIP": (1.3, 0)}

        class _Conn:
            def execute(self, sql, params):
                key = params[0]
                class _R:
                    def fetchone(_s):
                        return store.get(key)
                return _R()
            def __enter__(self): return self
            def __exit__(self, *a): return False

        monkeypatch.setattr(ch, "db", lambda: _Conn())
        return store

    def test_the_wrapped_name_finds_the_bare_row(self, rows):
        mult, paused = ch.get_channel_lot_mult("Telegram Auto (Gold Diggers VIP)")

        assert mult == 1.3

    def test_the_bare_name_still_works(self, rows):
        assert ch.get_channel_lot_mult("Gold Diggers VIP")[0] == 1.3

    def test_an_unknown_channel_is_neutral(self, rows):
        """1.0 and not paused -- an unknown channel must not be scaled or
        blocked."""
        assert ch.get_channel_lot_mult("Nobody") == (1.0, False)

    def test_no_source_is_neutral(self, rows):
        assert ch.get_channel_lot_mult("") == (1.0, False)
