"""`compute_mt5_performance` is a REPORTING call. Nothing may decide a trade on it.

Measured 2026-09-10 with the page open: `/history?days=90` ran **31.5 times a
minute**, the largest single source of bridge traffic left after the History
panels were coalesced. It is not the History panels — it is this function,
called independently by the AI Summary page, the History page, the Trading
page, the Telegram `!report` command and the email scheduler, each on its own
refresh.

Every one of those composes something a human reads. **None of them decides a
trade**, and this file exists to keep it that way.

**Why it is pinned.** A cache was attempted here on 2026-09-10 to collapse that
31.5/min, on exactly this reasoning. It was reverted: a module-level cache
keyed only by `days` is shared across bridges, and eight existing tests caught
the interference immediately. The reasoning was sound and the implementation
was not.

If it is attempted again it needs keying by bridge identity, not just period —
and it still needs this assertion, because the moment something on the trading
path reads these figures, a shared result stops being a latency fix and becomes
stale data behind a decision.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]

# Modules that decide, place, size or manage a trade. None may call this.
TRADING_PATH = (
    "services/trading/open_trade.py",
    "services/trading/open_from_signal.py",
    "services/trading/scan_auto_execute.py",
    "services/trading/instant_entry.py",
    "services/trading/limit_order_signal.py",
    "services/trading/close_trade.py",
    "services/positions/monitor_cycle.py",
    "services/signals/resolution.py",
    "services/signals/pending_activation.py",
    "services/risk/governor.py",
)


class TestItIsOnlyEverUsedForReporting:
    @pytest.mark.parametrize("rel", TRADING_PATH)
    def test_no_trading_path_module_calls_it(self, rel):
        path = REPO / "backend" / "src" / rel
        if not path.exists():
            pytest.skip(f"{rel} has moved")
        src = path.read_text(encoding="utf-8")

        assert "compute_mt5_performance" not in src, (
            f"{rel} calls compute_mt5_performance, which is CACHED on the "
            f"basis that nothing deciding a trade uses it. Either stop calling "
            f"it there or remove the cache."
        )

    def test_the_known_callers_are_all_reporting(self):
        """A whole-repo sweep, so a NEW caller anywhere has to be looked at."""
        # `frontend/` was a search root until 2026-09-18. It holds TypeScript
        # now, and the three page callers listed below went with the NiceGUI
        # tabs that held them. They will come back as routers under
        # backend/src/api/ when those tabs are ported (react-port task 080) —
        # which this sweep already covers, because it walks all of backend/src.
        out = subprocess.run(
            ["grep", "-rln", "--include=*.py", "compute_mt5_performance",
             str(REPO / "backend" / "src")],
            capture_output=True, text=True).stdout.split()
        found = {Path(p).relative_to(REPO).as_posix() for p in out
                 if "__pycache__" not in p}
        expected = {
            "backend/src/runtime.py",
            "backend/src/services/broker/mt5_performance.py",
            "backend/src/services/trading/bot_trading.py",       # !report
            "backend/src/services/notifications/scheduler.py",   # email
            # The Analysis tab's one consolidated read (2026-09-18). Reporting:
            # it renders the account's headline numbers for a chosen window and
            # decides nothing. It is also the ONLY broker call that tab makes —
            # see tests/api/routers/test_history.py, which pins that a full
            # render costs one round-trip rather than the 4.3-a-minute the
            # NiceGUI page cost (bugs/030).
            "backend/src/api/routers/history.py",
            # The AI Analysis tab's market research (2026-09-20). Reporting,
            # and the same caller the NiceGUI AI Summary page in this list's
            # history was: the figures go into the prompt the model reads.
            # It decides nothing, places nothing, and sizes nothing -- the
            # answer is prose on a screen a human then acts on, or does not.
            "backend/src/services/ai/market_research.py",
        }

        assert found == expected, (
            f"the set of callers changed: added {sorted(found - expected)}, "
            f"gone {sorted(expected - found)}. Check the new one only reports."
        )
