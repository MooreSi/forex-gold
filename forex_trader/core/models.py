"""Data models shared across the application."""

from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Tick:
    bid: float
    ask: float
    mid: float
    spread: float
    spread_points: float
    timestamp: float
    source: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Signal:
    signal_id: str
    source_name: str
    direction: str
    entry_low: float
    entry_high: float
    stop_loss: float
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp4: Optional[float] = None
    tp5: Optional[float] = None
    tp6: Optional[float] = None
    tp7: Optional[float] = None
    tp8: Optional[float] = None
    lot_size: Optional[float] = None
    risk_pct: Optional[float] = None
    notes: str = ""
    status: str = "pending"
    created_at: float = 0.0
    activated_at: Optional[float] = None
    cancelled_at: Optional[float] = None
    claude_commentary: Optional[str] = None


@dataclass
class Trade:
    trade_id: str
    signal_id: str
    direction: str
    entry_low: float
    entry_high: float
    entry_price: float
    lot_size: float
    remaining_lots: float
    stop_loss: float
    status: str = "open"
    open_time: float = 0.0
    mt5_ticket: Optional[int] = None
    tp1: Optional[float] = None
    tp2: Optional[float] = None
    tp3: Optional[float] = None
    tp4: Optional[float] = None
    tp5: Optional[float] = None
    tp6: Optional[float] = None
    tp7: Optional[float] = None
    tp8: Optional[float] = None
    close_time: Optional[float] = None
    close_price: Optional[float] = None
    exit_reason: Optional[str] = None
    gross_pnl: float = 0.0
    realised_pnl: float = 0.0
    net_pnl: float = 0.0
    spread_cost: float = 0.0
    commission: float = 0.0
    swap_est: float = 0.0
    slippage_cost: float = 0.0
    mt5_profit: Optional[float] = None
    strategy: str = "scale_out"
    sl_moved_to_be: int = 0
    tg_source: Optional[str] = None


@dataclass
class TelegramMessage:
    id: int
    telegram_message_id: str
    group_id: str
    group_name: str
    sender_id: Optional[str]
    sender_name: str
    timestamp: Optional[str]
    received_at: str
    text: str
    raw_text: str
    has_media: bool = False
    media_type: str = "none"
    reply_to_message_id: Optional[str] = None
    forwarded: bool = False


MAX_TP = 8  # Maximum number of take-profit levels supported

# Strategy identifiers
STRATEGY_SCALE_OUT       = "scale_out"
STRATEGY_BE_RUNNER       = "be_runner"
STRATEGY_TRAIL_STOP      = "trail_stop"
STRATEGY_PROTECTED_SCALE = "protected_scale"
STRATEGY_CONSERVATIVE       = "conservative"
STRATEGY_NO_SL_SCALE        = "no_sl_scale"
STRATEGY_CONSERVATIVE_TRIAL = "conservative_trial"
STRATEGY_SCALP_RUNNER        = "scalp_runner"
STRATEGY_SIGNAL_CLIMBER      = "signal_climber"
STRATEGY_GDC                 = "gold_diggers_copy"
STRATEGY_GD_VIP_RUNNER        = "gd_vip_runner"
STRATEGY_ORB_FIXED           = "orb_fixed"
STRATEGY_ADAPTIVE_RUNNER      = "adaptive_runner"
STRATEGY_ADAPTIVE_RUNNER_2    = "adaptive_runner_2"

STRATEGY_NAMES = {
    STRATEGY_SCALE_OUT:          "Scale Out + Breakeven",
    STRATEGY_BE_RUNNER:          "Breakeven Runner",
    STRATEGY_TRAIL_STOP:         "Trailing Stop",
    STRATEGY_PROTECTED_SCALE:    "Protected Scale",
    STRATEGY_CONSERVATIVE:       "Conservative",
    STRATEGY_NO_SL_SCALE:        "Trend Ratchet",
    STRATEGY_CONSERVATIVE_TRIAL: "Conservative Trial",
    STRATEGY_SCALP_RUNNER:       "Scalp Runner",
    STRATEGY_SIGNAL_CLIMBER:     "Signal Climber",
    STRATEGY_GDC:                "Gold Diggers VIP Copy",
    STRATEGY_GD_VIP_RUNNER:      "GD VIP Runner",
    STRATEGY_ORB_FIXED:          "ORB/IVB Fixed",
    STRATEGY_ADAPTIVE_RUNNER:    "Adaptive Runner",
    STRATEGY_ADAPTIVE_RUNNER_2:  "Adaptive Runner 2",
}

STRATEGY_DESCRIPTIONS = {
    STRATEGY_SCALE_OUT: (
        "**Scale Out + Breakeven** — the default, balanced approach.\n\n"
        "Uses a tiered close schedule so the heaviest profit-booking happens early, "
        "then progressively smaller clips keep exposure running for bigger targets. "
        "After TP1 hits the SL moves to entry — the trade cannot lose money from that point.\n\n"
        "**Close schedule:** TP1 = 40%, TP2 = 30%, TP3 = 20%, TP4 = 10%, TP5+ = all remaining\n\n"
        "**Example** (0.10 lot, 5 TPs at 2530 / 2545 / 2558 / 2570 / 2585, entry 2510):\n"
        "- TP1 hit @ 2530: close **0.04 lots** (+$80), SL → 2510 (breakeven, risk-free)\n"
        "- TP2 hit @ 2545: close **0.03 lots** (+$105)\n"
        "- TP3 hit @ 2558: close **0.02 lots** (+$96)\n"
        "- TP4 hit @ 2570: close **0.01 lots** (+$60)\n"
        "- TP5 hit @ 2585: close final **0.00 lots** → any remainder closes in full (+$75 if runner survives)\n\n"
        "**Best for:** consistent markets, traders who want the bulk of profit banked early "
        "with a small runner left to capture extended moves — zero downside after TP1."
    ),
    STRATEGY_BE_RUNNER: (
        "**Breakeven Runner** — hold everything, let the SL do the work.\n\n"
        "No partial closes. The full position runs until the SL is eventually triggered. "
        "After each TP is cleared, the SL steps up to the *previous* TP level, locking in that profit.\n\n"
        "**ADX gate (ADX > 25 required):** in ranging conditions (ADX < 25) this strategy "
        "automatically falls back to **Scale Out** behaviour — banking partial profits rather "
        "than letting the full position give back gains as price chops sideways.\n\n"
        "**Example** (0.10 lot, 3 TPs at 2530 / 2545 / 2560, entry 2510):\n"
        "- TP1 cleared @ 2530: SL moves to 2510 (entry) — trade cannot lose\n"
        "- TP2 cleared @ 2545: SL steps up to 2530 (TP1 locked in)\n"
        "- TP3 cleared @ 2560: SL steps up to 2545 (TP2 locked in)\n"
        "- Price reverses to 2545: full 0.10 lots exit with $350 profit\n\n"
        "**Best for:** trending markets (ADX > 25) where strong directional momentum "
        "is confirmed — maximises exposure to big moves without taking partial profits early."
    ),
    STRATEGY_TRAIL_STOP: (
        "**Trailing Stop** — follow the price, exit on the first reversal.\n\n"
        "No partial closes. Once TP1 is reached a trailing stop activates that follows "
        "price upward by a fixed dollar distance and only ever moves in the profitable direction.\n\n"
        "**Example** (trail distance $5.00, BUY from 2510, TP1=2530):\n"
        "- Price reaches 2530: trailing stop activates at 2525 ($5 behind)\n"
        "- Price climbs to 2550: SL trails up to 2545\n"
        "- Price peaks at 2560: SL at 2555\n"
        "- Price reverses to 2555: full position exits at $450 profit\n\n"
        "**Trail distance** is set below — smaller = tighter, exits sooner; larger = more breathing room.\n\n"
        "**Best for:** momentum trades with no clear TP target — captures as much of the move as possible."
    ),
    STRATEGY_PROTECTED_SCALE: (
        "**Protected Scale** — wait for confirmation, then scale out aggressively.\n\n"
        "Holds the full position through TP1 and TP2 with no closes. "
        "At TP2 the SL moves to entry (breakeven). From TP3 onwards, 20% is closed at each level.\n\n"
        "**Example** (0.10 lot, 5 TPs: 2530 / 2545 / 2558 / 2570 / 2585, entry 2510):\n"
        "- TP1 hit @ 2530: *no close*, SL stays at original stop\n"
        "- TP2 hit @ 2545: *no close*, SL moves to 2510 (entry) — now risk-free\n"
        "- TP3 hit @ 2558: close 0.02 lots\n"
        "- TP4 hit @ 2570: close 0.02 lots\n"
        "- TP5 hit @ 2585: close 0.02 lots — 0.04 lots remain as runner (exits at SL)\n\n"
        "**Best for:** high-conviction setups where TP1/TP2 are expected to be swept quickly "
        "before the real move develops — accepts full risk to TP2, then protects aggressively."
    ),
    STRATEGY_CONSERVATIVE: (
        "**Conservative** — fixed 5-pt SL / 3-pt TP1, ignore signal levels, trail the runner.\n\n"
        "Signal SL and TP levels from Telegram are used only to fill the trade at the entry zone. "
        "Once filled, all signal levels are discarded and replaced with fixed distances from the actual fill price:\n\n"
        "- **SL:** 5 pts adverse from fill (e.g. fill at 3315 BUY → SL at 3310)\n"
        "- **TP1:** 3 pts in profit from fill (e.g. fill at 3315 BUY → TP1 at 3318)\n\n"
        "**At TP1:** close 80% of the position. "
        "SL moves immediately to the fill price (breakeven) — the trade cannot lose money from this point.\n\n"
        "**After TP1:** the remaining 20% is managed with a **3-pt trailing stop** that follows "
        "price in the profitable direction and only ever moves in your favour. "
        "The trail is floored at breakeven (fill price) so the runner is always protected.\n\n"
        "**Example** (0.20 lot, BUY fill at 3315.00):\n"
        "- SL placed at 3310.00 (−5 pts)\n"
        "- TP1 placed at 3318.00 (+3 pts)\n"
        "- Price reaches 3318 → 0.16 lots closed, **SL → 3315.00 (breakeven)**\n"
        "- Price climbs to 3323 → trailing SL at 3320.00\n"
        "- Price climbs to 3328 → trailing SL at 3325.00\n"
        "- Price reverses to 3325 → 0.04 lots close at 3325.00 (+10 pts on runner)\n\n"
        "Lot size is calculated from the fixed 5-pt SL distance, not the signal SL — "
        "ensuring consistent risk per trade regardless of signal quality.\n\n"
        "**Best for:** tight scalps where TP1 is the primary target and the trail "
        "captures any extended move risk-free. Works with any signal — signal TP/SL quality irrelevant."
    ),
    STRATEGY_NO_SL_SCALE: (
        "**Trend Ratchet** — ride a confirmed trend with a ratcheting lock-in mechanism.\n\n"
        "**ADX gate (ADX > 30 required at entry):** only opens in confirmed trending conditions. "
        "If ADX < 30 when the signal fires the trade is blocked — preventing entries into ranging markets "
        "where the wide emergency stop would be punished repeatedly.\n\n"
        "Instead of the signal's SL, an **emergency stop** is placed at **1.5×** the signal's SL distance — "
        "wide enough to survive short-term spikes, tight enough to limit damage if the trend fails. "
        "20% closes at TP1 and TP3 lock in early profit while preserving 60% exposure. "
        "At TP3 the SL ratchets up to the **TP1 level** — the trade is now running on banked profit. "
        "From TP4 onwards the SL advances by two TP levels at each milestone "
        "(TP4→TP2, TP5→TP3, TP6→TP4, TP7→TP5), continuously locking in escalating profit. "
        "All remaining lots close at the **last TP of the signal** (maximum TP8).\n\n"
        "**Example — 8-TP signal** (0.10 lot, entry 4150, signal SL 4140, emergency SL 4135):\n"
        "- Entry opens with SL at 4135 (1.5× the 10-pt signal SL distance)\n"
        "- TP1 hit @ 4158: close 0.02 lots — first profit banked\n"
        "- TP2 hit @ 4162: *skipped*\n"
        "- TP3 hit @ 4166: close 0.02 lots, SL ratchets → 4158 (TP1 — profit locked)\n"
        "- TP4 hit @ 4170: SL ratchets → 4162 (TP2 level)\n"
        "- TP5 hit @ 4175: SL ratchets → 4166 (TP3 level)\n"
        "- TP6 hit @ 4182: SL ratchets → 4170 (TP4 level)\n"
        "- TP7 hit @ 4190: SL ratchets → 4175 (TP5 level)\n"
        "- TP8 hit @ 4200: close final 0.06 lots — trade fully closed\n\n"
        "**Example — 5-TP signal** (same entry, TPs: 4158/4162/4166/4170/4175):\n"
        "- TP1 hit @ 4158: close 0.02 lots\n"
        "- TP2 hit @ 4162: *skipped*\n"
        "- TP3 hit @ 4166: close 0.02 lots, SL → 4158 (TP1)\n"
        "- TP4 hit @ 4170: SL → 4162 (TP2)\n"
        "- TP5 hit @ 4175: close final 0.06 lots — trade fully closed\n\n"
        "**Best for:** confirmed trending markets (ADX > 30) with GDV-style multi-TP signals — "
        "avoids whipsaw stops on minor pullbacks while progressively locking in profit through the full trend move."
    ),
    STRATEGY_CONSERVATIVE_TRIAL: (
        "**Conservative Trial** — fixed SL/TP from fill price; ignores signal levels entirely.\n\n"
        "Uses GD2 and GD VIP signal direction only. Sets its own SL (max $100 loss = 10 pts at 0.1 lot) "
        "and 6 TP levels calculated from the actual fill price:\n\n"
        "- TP1 +5 pts → 5% close (early insurance)\n"
        "- TP2 +10 pts → 30% close + **SL moves to entry (breakeven)**\n"
        "- TP3 +14 pts → 20% close\n"
        "- TP4 +20 pts → 40% close + **SL steps to TP2 level**\n"
        "- TP5 +27 pts → 5% close\n"
        "- TP6 +35 pts → close all remaining\n\n"
        "Signal SL/TP from Telegram are completely ignored — only the direction is used. "
        "This makes the strategy safe to run unattended regardless of signal quality."
    ),
    STRATEGY_SCALP_RUNNER: (
        "**Scalp Runner** — two-stage scalp: a small insurance close at TP1, "
        "full breakeven protection and a trailing runner from TP2 onward.\n\n"
        "Signal SL and TP levels from Telegram are used only to fill the trade at the entry zone. "
        "Once filled, all signal levels are discarded and replaced with fixed distances from the actual fill price:\n\n"
        "- **SL:** 10 pts adverse from fill (e.g. fill at 3315 BUY → SL at 3305)\n"
        "- **TP1:** 3 pts in profit from fill (e.g. fill at 3315 BUY → TP1 at 3318)\n"
        "- **TP2:** 4 pts in profit from fill (e.g. fill at 3315 BUY → TP2 at 3319)\n\n"
        "**At TP1:** close 50% of the position. SL is left untouched — the trade is still risking "
        "the full 10-pt stop on the remaining half at this point.\n\n"
        "**At TP2:** SL moves to the fill price (breakeven) — the trade cannot lose money from "
        "this point — and the 3-pt trailing stop on the remaining 50% begins.\n\n"
        "**After TP2:** the remaining 50% is managed with a **3-pt trailing stop** that follows "
        "price in the profitable direction and only ever moves in your favour. "
        "The trail is floored at breakeven (fill price) so the runner is always protected.\n\n"
        "**Example** (0.20 lot, BUY fill at 3315.00):\n"
        "- SL placed at 3305.00 (−10 pts)\n"
        "- TP1 placed at 3318.00 (+3 pts), TP2 placed at 3319.00 (+4 pts)\n"
        "- Price reaches 3318 → 0.10 lots closed, SL unchanged at 3305.00\n"
        "- Price reaches 3319 → **SL → 3315.00 (breakeven)**, trailing starts\n"
        "- Price climbs to 3325 → trailing SL at 3322.00\n"
        "- Price climbs to 3330 → trailing SL at 3327.00\n"
        "- Price reverses to 3327 → 0.10 lots close at 3327.00 (+12 pts on runner)\n\n"
        "Compared to Conservative (80/20 split at a single TP), Scalp Runner risks the full stop "
        "for slightly longer (until TP2, not TP1) but keeps a full 50% position on the trail — "
        "more upside on strong moves once breakeven is locked in.\n\n"
        "Lot size is calculated from the fixed 10-pt SL distance, not the signal SL — "
        "ensuring consistent risk per trade regardless of signal quality.\n\n"
        "**Best for:** tight scalps where TP1 is reliable but the session has room for "
        "an extended follow-through move — the wider SL and two-stage confirmation trade a bit "
        "more initial risk for a cleaner breakeven trigger than Conservative's single-TP model."
    ),
    STRATEGY_GDC: (
        "**Gold Diggers VIP Copy** — trades signals generated by the GD Copy Engine.\n\n"
        "The GD Copy Engine reverse-engineers the Gold Diggers VIP methodology: Asia range, "
        "swing highs/lows, and round-number S/R levels are combined into a scored level list. "
        "When price approaches a top-ranked level, a pending limit zone is placed.\n\n"
        "**Signal structure mirrors GD VIP exactly:**\n"
        "- Entry zone: 2-4pt wide, centred on the identified S/R level\n"
        "- SL: 4-7pts below zone (score-dependent)\n"
        "- TP cascade: TP1 +3pts, TP2 +4pts, ... TP7 +15pts, TP8 +30pts\n"
        "- Session filter: 05:00-15:00 UTC only\n\n"
        "**Best for:** use alongside or instead of GD VIP Telegram signals to reduce "
        "latency — signals are placed pre-emptively before GD VIP posts. "
        "The correlation panel shows how accurately our signals predict GD VIP's entries."
    ),
    STRATEGY_GD_VIP_RUNNER: (
        "**GD VIP Runner** — wide-SL ladder built from a backtest of 259 Gold Diggers VIP "
        "signals (May-Jun 2026), designed to stop the channel's own stated SL from cutting off "
        "trades that go on to reach much higher TP levels.\n\n"
        "**Finding the strategy is built on:** GD VIP's posted SL is sized for a TP1-only scalp "
        "(median ≈4.8pt, about the same as the TP1 distance) — not for the multi-TP move the "
        "signals statistically achieve. Unconditionally, signals reach TP1-3 100% of the time, "
        "but the *stated* SL pre-empts TP1 in 41.7% of cases before price ever gets there. "
        "Backtest: baseline (stated SL, full close @ TP1) averages -0.127R/trade; this strategy's "
        "ladder averages +0.167R/trade at 88.7% win rate, with no trade losing more than its "
        "initial defined risk (worst case = -1.00R).\n\n"
        "**Mechanism — keeps the signal's own TP ladder, widens only the SL:**\n"
        "- SL: min(4 × signal's stated SL distance, 20pt) from fill — never the raw stated SL\n"
        "- TP1-8: used exactly as the signal sent them (no override)\n"
        "- Close schedule: TP1-2 = 5% each, TP3-4 = 10% each, TP5-7 = 15% each, TP8 = 25%\n"
        "- SL trails to breakeven after TP1, then to the previous TP price after every "
        "subsequent TP — never moves against the trade\n\n"
        "**Extended pending-fill window:** GD VIP zone signals often take over an hour to be "
        "touched (median ≈101 min in the backtest sample) — while this strategy is selected, "
        "pending signals are kept live for up to 4 hours instead of the default 2-minute "
        "expiry, since cancelling them early would discard most of the validated edge.\n\n"
        "**Scope:** tuned specifically on Gold Diggers VIP signal behaviour (excludes Gold "
        "Diggers 2.0), but the mechanism itself does not check signal source — it can be applied "
        "to any channel whose signals carry a full TP ladder.\n\n"
        "**Best for:** Gold Diggers VIP zone-entry signals specifically — not intended for "
        "scalp-only channels with a single TP."
    ),
    STRATEGY_SIGNAL_CLIMBER: (
        "**Signal Climber** — rides the signal provider's full TP ladder with progressive exits.\n\n"
        "Unlike Conservative and Scalp Runner (which discard signal levels and use fixed offsets), "
        "Signal Climber respects the signal exactly as sent: the signal's own SL and all TP levels "
        "are used as-is. Lot size is calculated from the signal's actual SL distance.\n\n"
        "**Exit schedule (fractions of original lot):**\n"
        "- TP1: 20% close → SL moves to entry (breakeven — trade cannot lose)\n"
        "- TP2: 15% close → SL trails to TP1 price\n"
        "- TP3: 15% close → SL trails to TP2 price\n"
        "- TP4: 15% close → SL trails to TP3 price\n"
        "- TP5: 20% close → SL trails to TP4 price\n"
        "- TP6+: all remaining → SL trails to TP5 price\n\n"
        "For signals with fewer TPs the fractions scale proportionally "
        "(1 TP = 100% at TP1; 3 TPs = 30/30/40; 4 TPs = 20/25/25/30).\n\n"
        "**Example** (0.10 lot BUY, GD2 signal: entry 4048-4053, SL 4044, "
        "TP1=4056 / TP2=4058 / TP3=4060 / TP4=4062 / TP5=4065 / TP6=4090):\n"
        "- Entry fills at 4053; SL at 4044 (9 pts risk, lot sized to $1 risk per pt)\n"
        "- TP1 hit @ 4056 (+3 pts): 0.02 lots closed (+$6), **SL → 4053 (breakeven)**\n"
        "- TP2 hit @ 4058 (+5 pts): 0.015 lots closed (+$7.50), SL → 4056 (TP1)\n"
        "- TP3 hit @ 4060 (+7 pts): 0.015 lots closed (+$10.50), SL → 4058 (TP2)\n"
        "- TP4 hit @ 4062 (+9 pts): 0.015 lots closed (+$13.50), SL → 4060 (TP3)\n"
        "- TP5 hit @ 4065 (+12 pts): 0.02 lots closed (+$24), SL → 4062 (TP4)\n"
        "- TP6 hit @ 4090 (+37 pts): 0.015 lots closed (+$55.50) — trade done\n"
        "- **Total: +$117 on a 9-pt signal SL** (vs Conservative's ~$36 max)\n\n"
        "If SL triggers after TP3 (SL at TP2=4058): remaining 0.05 lots close at 4058 (+$5). "
        "Combined with earlier partials the trade is profitable even on an 'early exit'.\n\n"
        "**Best for:** professional multi-TP signals (Gold Diggers 2.0, Gold Diggers VIP) where "
        "the signal provider intends TP5/TP6 as the real target — maximises the benefit of "
        "their full analysis while the trailing SL locks in profit at each level."
    ),
    STRATEGY_ORB_FIXED: (
        "**ORB/IVB Fixed** — no management beyond the setup itself.\n\n"
        "Used only by the ORB/IVB Report tab's Execute Trade button. The stop "
        "and target are taken exactly as computed by the London opening-range "
        "reload-zone setup and never recalculated, moved to breakeven, or "
        "trailed — full close at the target, full close at the stop, nothing "
        "in between.\n\n"
        "**Best for:** trading the ORB/IVB report's own recommendation "
        "unmodified — not intended as a general-purpose strategy choice."
    ),
    STRATEGY_ADAPTIVE_RUNNER: (
        "**Adaptive Runner** — widens the SL like GD VIP Runner, but caps the widened "
        "distance against the signal's own reachable reward, so the strategy no longer "
        "needs to know in advance whether it's looking at a full GD-VIP-style 8-TP ladder "
        "or a short 1-3 TP scalp signal.\n\n"
        "**Problem this fixes:** GD VIP Runner's fixed 4x/20pt SL widening was validated "
        "against Gold Diggers VIP's own signal shape (tiny ~4-5pt stated SL, real target "
        "~25-30pt out at TP8) — there the widened stop is a small fraction of the reachable "
        "reward. Applied to a shorter-ladder signal (e.g. a 3-TP Breakout/Bounce signal with "
        "an 8pt stated SL and a ~15pt final target) the same formula widens the SL to 20pt — "
        "*wider than the maximum possible win* — which is a structurally losing trade "
        "regardless of win rate.\n\n"
        "**Mechanism:**\n"
        "- SL: min(4 × stated SL distance, 20pt) same as GD VIP Runner, then capped again at "
        "50% of the distance to the signal's final (furthest) TP — but never tightened below "
        "the signal's own stated SL. If the final TP isn't far enough past the stated SL to "
        "support any widening, the stated SL is used as-is instead of forcing extra room that "
        "isn't there.\n"
        "- TP1-8: used exactly as the signal sent them, closing fractions scaled to however "
        "many TPs the signal actually has (same shape as GD VIP Runner/Signal Climber — no "
        "fixed 8-slot ladder applied to a 3-TP signal, which silently drops most of the "
        "position with nothing to close it).\n"
        "- SL moves to breakeven at TP1 (not delayed to TP2 like GD VIP Runner) — the stop is "
        "now proportionate to the target, so there's no need to keep full risk on the table "
        "past the first target.\n\n"
        "**Best for:** channels/signals of unknown or mixed TP-ladder length, where a single "
        "wide-SL formula tuned for one signal shape would be wrong for another."
    ),
    STRATEGY_ADAPTIVE_RUNNER_2: (
        "**Adaptive Runner 2** — fixed 10pt SL, GD VIP Runner's back-loaded close "
        "schedule, and a lagging-midpoint trail instead of trail-to-previous-TP.\n\n"
        "Signal SL is ignored entirely — the SL is always exactly 10pts from the "
        "actual fill price, regardless of the signal's stated SL or TP spread. "
        "TP levels are kept exactly as the signal sent them.\n\n"
        "**Close schedule:** TP1 = 5%, TP2 = 5%, TP3 = 10%, TP4 = 10%, TP5 = 15%, "
        "TP6 = 15%, TP7 = 15%, TP8 = all remaining (same table as GD VIP Runner, "
        "scaled the same way for signals with fewer than 8 TPs).\n\n"
        "**SL trail — the part that's different from every other ladder strategy:**\n"
        "- TP1: no SL change (still the fixed 10pt stop)\n"
        "- TP2: SL → entry (breakeven)\n"
        "- TP3 onward: SL → the **midpoint of the two TPs before the one just hit** "
        "(TP3 → mid(TP1, TP2), TP4 → mid(TP2, TP3), TP5 → mid(TP3, TP4), and so on) — "
        "not the single previous TP price the other ladder strategies use. This keeps "
        "roughly a two-TP-wide cushion behind price instead of snapping the stop right "
        "up to the last level cleared, trading a bit of give-back room for fewer "
        "single-tick stop-outs on minor pullbacks between levels.\n\n"
        "**Example** (0.10 lot, 5 TPs: 4056 / 4058 / 4060 / 4062 / 4065, fill 4053, "
        "10pt SL at 4043 — 5-TP close schedule is 10/10/15/25/remaining):\n"
        "- TP1 hit @ 4056: 0.01 lots closed, SL unchanged at 4043\n"
        "- TP2 hit @ 4058: 0.01 lots closed, **SL → 4053 (breakeven)**\n"
        "- TP3 hit @ 4060: 0.015 lots closed, SL → mid(4056, 4058) = 4057\n"
        "- TP4 hit @ 4062: 0.025 lots closed, SL → mid(4058, 4060) = 4059\n"
        "- TP5 hit @ 4065: close final 0.04 lots — trade fully closed\n\n"
        "**Best for:** signals where a flat 10pt stop is an acceptable, predictable "
        "risk regardless of the signal's own SL quality, and where a two-level trail "
        "cushion is preferred over snapping the stop to the immediately-prior TP."
    ),
}

# ── Local timezone ────────────────────────────────────────────────────────────
# Reflects the computer's current UTC offset (updates on DST boundary at restart).
# Used by all UI display helpers and daily-stat cutoffs so the app respects
# whatever timezone the machine is configured to.
from datetime import datetime as _dt_tz, timezone as _tz_base
LOCAL_TZ = _dt_tz.now(_tz_base.utc).astimezone().tzinfo
del _dt_tz, _tz_base  # keep namespace clean

# ── Contract specification for XAUUSD ─────────────────────────────────────────
SYMBOL         = "XAUUSD"
CONTRACT_SIZE  = 100.0   # oz per standard lot
POINT_SIZE     = 0.01    # 1 point = $0.01
DIGITS         = 2
