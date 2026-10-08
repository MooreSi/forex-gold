"""A Resume forgives the daily-loss limit an earlier loss, never an earlier profit.

Owner, 2026-10-08: the day was +$37.87 by 13:35, trading was resumed (from the
daily goal's halt), and -$63.84 came after. The day stood at -$25.97 on MT5 and
the header read -$33.61, yet trading stopped on "Daily loss limit hit: $-63.84
today vs -$63.28 (10.0% of $632.84)": the limit's window started at the Resume
and the morning's profit fell out of it.

The mirror of `daily_goal.goal_figure`: the limit counts realised P&L since the
window opened PLUS any profit made earlier in the broker day. A loss before
the Resume is still forgiven, which is what makes Resume after this halt mean
anything (`test_giveback_guard.test_resuming_also_re_arms_the_daily_loss_ceiling`).

Nothing here reaches a broker: closes are local rows and the balance is passed in.
"""
import time

from backend.src.services.risk import governor as rg

from tests.core.test_giveback_guard import _closes

DL = {"max_daily_loss_pct": 10.0}


def test_profit_before_a_resume_still_cushions_the_day(fresh_db):
    """The owner's day: +37.87, Resume, -63.84. The day is -25.97: no halt."""
    _closes([7.87, -0.23, 5.04, 13.47, 11.72])
    rg.rearm_risk_guards()
    _closes([-15.87, -11.92, -12.76, 5.14, 5.08, 7.10, 11.49,
             -9.62, -9.92, -12.58, -10.00, -9.98], at=time.time() + 1)
    assert rg.check_daily_loss_limit(DL, balance=569.00) is None


def test_the_day_as_a_whole_still_hits_the_limit(fresh_db):
    """+20, Resume, -120: the day is -100 on a 1000 opening, exactly 10%."""
    _closes([20.0])
    rg.rearm_risk_guards()
    _closes([-120.0], at=time.time() + 1)
    reason = rg.check_daily_loss_limit(DL, balance=900.0)
    assert reason is not None
    assert "$-100.00 today" in reason and "10.0% of $1,000.00" in reason


def test_just_inside_the_limit_counting_the_profit(fresh_db):
    _closes([20.0])
    rg.rearm_risk_guards()
    _closes([-119.0], at=time.time() + 1)   # day -99 on 1000: under 10%
    assert rg.check_daily_loss_limit(DL, balance=901.0) is None
