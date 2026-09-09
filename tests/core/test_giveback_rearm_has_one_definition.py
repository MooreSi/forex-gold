"""Re-arming the give-back guard must have one definition, and be reachable.

Found 2026-09-09 auditing which governor functions anything can actually reach.

`rearm_giveback_guard()` writes `giveback_baseline_ts` and says of itself:

    Called whenever trading is resumed by hand.

**Nothing called it.** `rearm_risk_guards()` writes the same key, plus
`daily_loss_baseline_ts`, and that is the one the Resume paths use
(`core_bot_panel`, `bot_readonly`). So the give-back half of a manual resume
worked, but through a second copy of the write, while the function documented
as owning it sat unreachable with a docstring that was no longer true.

Nothing was broken today. It is the shape that matters: two writers of one
baseline, one of them unused, is how the pair drifts the next time either
changes — the same failure the bias gate had with six call sites and the IME
gate had with three.

`rearm_risk_guards` now delegates, so there is one definition. Both writes
still land in one transaction: `db_module.db()` is re-entrant and the inner
call joins the outer block.
"""
from __future__ import annotations

import inspect

from backend.src.services.risk import governor


class TestOneDefinition:
    def test_rearm_risk_guards_delegates(self):
        body = "\n".join(
            l for l in inspect.getsource(governor.rearm_risk_guards).splitlines()
            if not l.strip().startswith("#")
        )

        assert "rearm_giveback_guard(" in body, (
            "rearm_risk_guards still writes giveback_baseline_ts itself, so "
            "there are two definitions of re-arming that guard"
        )

    def test_it_does_not_write_the_giveback_key_directly(self):
        body = "\n".join(
            l for l in inspect.getsource(governor.rearm_risk_guards).splitlines()
            if not l.strip().startswith("#")
        )

        assert "giveback_baseline_ts" not in body, (
            "the second writer is still there"
        )

    def test_the_daily_loss_baseline_is_still_reset(self):
        """The other half of what Resume has to do — easy to lose in this
        refactor, and losing it means a resume is undone by the next close."""
        body = inspect.getsource(governor.rearm_risk_guards)

        assert "daily_loss_baseline_ts" in body


class TestBothBaselinesActuallyMove:
    def test_a_rearm_sets_both(self, fresh_db):
        fresh_db.set_app_config("giveback_baseline_ts", "0")
        fresh_db.set_app_config("daily_loss_baseline_ts", "0")

        governor.rearm_risk_guards()

        assert float(fresh_db.get_app_config("giveback_baseline_ts")) > 0
        assert float(fresh_db.get_app_config("daily_loss_baseline_ts")) > 0

    def test_the_giveback_rearm_on_its_own_moves_only_its_own(self, fresh_db):
        """It is still a separate, callable thing — a caller that wants only
        the give-back window reset must not silently reset the daily-loss one
        as well."""
        fresh_db.set_app_config("giveback_baseline_ts", "0")
        fresh_db.set_app_config("daily_loss_baseline_ts", "0")

        governor.rearm_giveback_guard()

        assert float(fresh_db.get_app_config("giveback_baseline_ts")) > 0
        assert float(fresh_db.get_app_config("daily_loss_baseline_ts")) == 0
