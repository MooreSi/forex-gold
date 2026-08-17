"""Auto template selection -- which EA template each channel should be running
right now, given the live market regime.

WHY THIS EXISTS (2026-08-14)
---------------------------
A single template per channel is wrong on most days. The same channel's
signals behave very differently depending on whether gold is trending,
drifting, or ranging, and the geometry that wins in one regime is often the
worst available in another. Measured over 31 days of M5 data against every
signal each source actually produced:

  GD INSTITUTIONAL  trending   limit entry, SL40, 40/80/130   +0.281R (61% win)
                    weak_trend limit entry, SL35, 35/70       +0.421R (70% win)
                    ranging    limit entry, SL35, 35/70       +0.194R (55% win)
  GD VIP            trending   limit entry, SL50, trend ladder+0.243R (40% win)
                    weak_trend MARKET entry, SL50, trend ladder+0.276R (37% win)
                    ranging    -- no positive configuration exists --
  Reversal Engine   trending   market entry, SL40, 40/80/130  +0.122R (54% win)
                    weak_trend market entry, SL60, 60/120/200 +0.113R (39% win)
                    ranging    limit entry, SL40, 40/80/130   +0.065R (46% win)

Every one of those eight cells was positive in BOTH halves of the sample.
The ninth -- GD VIP in a ranging market -- was negative under all twelve
entry/geometry combinations tried, best case -0.084R, so it maps to
STAND_DOWN rather than to a least-bad template. Standing a channel down is
a first-class outcome here: the largest single improvement available in
this system has consistently been not taking the trade.

The mapping is deliberately a plain dict rather than anything learned at
runtime. It is the deterministic floor: cheap, auditable, unchanged when
the Anthropic API is slow or unreachable, and identical between the
backtest that produced it and the loop that consumes it. The AI layer
(channel_strategy_ai) may override a cell, but it starts from here and
falls back to here.

Regimes come from dpm_engine.detect_regime -- the SAME classifier the
backtest used, fed the same 30 M5 bars the engine already keeps in
_dpm_candles. "spike" is included for completeness but was observed in
0.1% of 30-minute samples over the period, far too rare to fit anything
to, so it inherits the most defensive cell each channel has rather than
getting a tuned entry of its own.
"""
from __future__ import annotations

import logging
from typing import Optional

log = logging.getLogger(__name__)

# Sentinel: this channel should not trade in this regime at all.
STAND_DOWN = "stand_down"

# Regimes dpm_engine.detect_regime can return.
REGIMES = ("trending", "weak_trend", "ranging", "spike")

# The six templates the mapping draws on -- created 2026-08-14 from the
# backtest above. Kept here as names (not definitions) because the
# templates themselves live in ea_trade_templates and stay user-editable.
LIMIT_SCALP     = "template:Auto Limit Scalp"
LIMIT_BALANCED  = "template:Auto Limit Balanced"
LIMIT_TREND     = "template:Auto Limit Trend"
MARKET_BALANCED = "template:Auto Market Balanced"
MARKET_TREND    = "template:Auto Market Trend"
MARKET_RUNNER   = "template:Auto Market Runner"

# (canonical channel/source name, regime) -> template override or STAND_DOWN.
#
# "spike" reuses each channel's tightest-risk cell: a volatility event is the
# one condition where being wrong is most expensive, and no cell has enough
# spike samples to justify anything bespoke.
_MAP: dict[tuple[str, str], str] = {
    ("GOLD DIGGERS INSTITUTIONAL", "trending"):   LIMIT_BALANCED,
    ("GOLD DIGGERS INSTITUTIONAL", "weak_trend"): LIMIT_SCALP,
    ("GOLD DIGGERS INSTITUTIONAL", "ranging"):    LIMIT_SCALP,
    ("GOLD DIGGERS INSTITUTIONAL", "spike"):      LIMIT_SCALP,

    ("Gold Diggers VIP", "trending"):   LIMIT_TREND,
    ("Gold Diggers VIP", "weak_trend"): MARKET_TREND,
    ("Gold Diggers VIP", "ranging"):    STAND_DOWN,
    ("Gold Diggers VIP", "spike"):      STAND_DOWN,

    ("Reversal Engine", "trending"):   MARKET_BALANCED,
    ("Reversal Engine", "weak_trend"): MARKET_RUNNER,
    ("Reversal Engine", "ranging"):    LIMIT_BALANCED,
    ("Reversal Engine", "spike"):      LIMIT_BALANCED,
}

# A channel with no measured history of its own (a newly added one, or the
# Breakout Engine) gets the most conservative shape rather than inheriting
# another channel's tuning.
_DEFAULT_BY_REGIME = {
    "trending":   MARKET_BALANCED,
    "weak_trend": MARKET_BALANCED,
    "ranging":    LIMIT_BALANCED,
    "spike":      LIMIT_BALANCED,
}


def regime_from_candles(candles: Optional[list]) -> str:
    """Live regime for a set of M5 candles, defaulting to the most defensive
    label when there isn't enough data to classify.

    Wraps dpm_engine.detect_regime so every caller -- the resolution gate,
    the auto-manage loop and the AI prompt -- goes through one place and
    cannot disagree about what regime it currently is. Defaults to "ranging"
    rather than "trending": with no data, the tighter cell is the safer
    assumption.
    """
    c = list(candles or [])
    if len(c) < 20:
        return "ranging"
    try:
        from forex_trader.core.dpm_engine import detect_regime, compute_atr
        return detect_regime(c, compute_atr(c))
    except Exception:
        return "ranging"


def _canon(source: str) -> str:
    """Fold a decorated source label back to the channel key used above.

    Stored sources arrive in several shapes -- "Telegram Auto (GOLD DIGGERS
    INSTITUTIONAL)", "instant:Gold Diggers VIP", or the bare channel name --
    so match on containment rather than equality, the same way the rest of
    the app's channel handling does.
    """
    s = (source or "").strip()
    up = s.upper()
    if "INSTITUTIONAL" in up:
        return "GOLD DIGGERS INSTITUTIONAL"
    if "VIP" in up:
        return "Gold Diggers VIP"
    if "REVERSAL" in up:
        return "Reversal Engine"
    if "BREAKOUT" in up:
        return "Breakout Engine"
    if "SCALP" in up:
        return "Gold Diggers Scalping"
    return s


def baseline_for(source: str, regime: str) -> str:
    """The backtested template override for `source` in `regime`, or
    STAND_DOWN. Never raises: an unknown channel or regime falls back to the
    conservative default rather than leaving the caller without an answer."""
    key = (_canon(source), regime)
    if key in _MAP:
        return _MAP[key]
    return _DEFAULT_BY_REGIME.get(regime, LIMIT_BALANCED)


def is_stand_down(choice: Optional[str]) -> bool:
    return (choice or "").strip().lower() == STAND_DOWN


def auto_templates() -> list[str]:
    """Every template override this module can select, for the AI's
    vocabulary and for validating whatever it returns."""
    return sorted({v for v in _MAP.values() if v != STAND_DOWN}
                  | set(_DEFAULT_BY_REGIME.values()))


def is_valid_auto_choice(choice: Optional[str]) -> bool:
    """Whether `choice` is something Auto mode is allowed to run today.

    Stored recommendations outlive the rules that produced them. When the
    built-in strategies stopped being selectable (2026-08-17), every channel
    holding a built-in in channel_strategy_rec kept trading it: the change
    only governed new AI responses, and nothing revalidated a row already in
    the database. GOLD DIGGERS INSTITUTIONAL ran "limit_runner" for nearly
    nine hours afterwards -- six trades at the global 0.1 lot rather than its
    template's configured 0.05 -- because the row simply never came up for
    reconsideration.

    So consumers check the stored value against the current vocabulary rather
    than trusting it, and fall back to the backtested baseline when it no
    longer qualifies. stand_down counts as valid: it is a real outcome, not a
    stale value.
    """
    c = (choice or "").strip()
    if not c:
        return False
    return is_stand_down(c) or c in set(auto_templates())


def auto_enabled_sources() -> list[str]:
    """Channels/engines currently set to Auto anywhere that matters.

    A source counts as Auto if EITHER its Channel Strategy pick is "auto" OR
    the trading schedule assigns "auto" to it in any window -- the schedule
    wins at signal time (core_signal_resolution), so a channel that is only
    auto inside one window still needs its recommendation kept fresh.
    """
    from forex_trader.core import database as _db
    from forex_trader.core import core_trading_schedule as _sched

    out: set[str] = set()
    try:
        for ch in _db.get_all_channel_parser_configs():
            name = ch.get("channel_name")
            if name and _db.get_channel_strategy_override(name) == "auto":
                out.add(name)
    except Exception:
        pass
    try:
        for _day, blocks in (_sched.get_trading_schedule() or {}).items():
            for b in blocks:
                for name, cfg in (b.get("telegram_channels") or {}).items():
                    if (cfg.get("strategy_override") or "") == "auto":
                        out.add(name)
                for key, label in (("reversal_engine_override", "Reversal Engine"),
                                   ("breakout_engine_override", "Breakout Engine")):
                    if (b.get(key) or "") == "auto":
                        out.add(label)
    except Exception:
        pass
    return sorted(out)


def apply_baselines(regime: str, sources: Optional[list[str]] = None,
                    force: bool = True) -> dict:
    """Write the deterministic baseline pick for `regime` into
    channel_strategy_rec for every Auto source, and return what changed.

    This is the non-AI half of auto management: it is what Auto falls back
    to when the API is unconfigured, rate-limited or down, and what asserts
    a new regime's geometry the moment the regime flips.

    `force` is what stops the two layers fighting. The detection loop runs
    every 60s but the AI reviews at most every 15 minutes, so re-asserting
    the baseline on every tick would revert each reasoned override within a
    minute of it being made -- observed live on the first run: the AI moved
    GOLD DIGGERS INSTITUTIONAL to stand_down and Gold Diggers VIP onto a
    template, and 60 seconds later the baseline pass put both back.

      force=True   regime changed (or first pass) -- the previous regime's
                   picks are stale by definition, so overwrite them.
      force=False  same regime -- only fill sources that have no usable
                   recommendation at all, and leave every existing pick
                   (baseline or AI) untouched.
    """
    from forex_trader.core import database as _db
    srcs = sources if sources is not None else auto_enabled_sources()
    known = set(auto_templates()) | {STAND_DOWN}
    changed: dict[str, tuple[str, str]] = {}
    for src in srcs:
        pick = baseline_for(src, regime)
        try:
            prev = (_db.get_channel_strategy_rec(src) or {}).get("strategy") or ""
        except Exception:
            prev = ""
        if prev == pick:
            continue
        # Within an unchanged regime, only seed a source that has nothing
        # usable yet -- never overwrite a live pick.
        if not force and prev in known:
            continue
        try:
            _db.set_channel_strategy_rec(src, pick, f"auto/{regime}: {describe_cell(src, regime)}", 0.6)
            changed[src] = (prev, pick)
        except Exception:
            log.debug("apply_baselines: could not store rec for %s", src, exc_info=True)
    return changed


def describe_cell(source: str, regime: str) -> str:
    """One-line explanation used in logs and in the AI prompt, so a switch is
    always traceable to the reason behind it."""
    ch = _canon(source)
    choice = baseline_for(source, regime)
    if is_stand_down(choice):
        return (f"{ch} in a {regime} market has no configuration with a positive "
                f"expectancy in backtest (best available -0.08R) -- stand down")
    return f"{ch} in a {regime} market -> {choice.split(':', 1)[-1]}"
