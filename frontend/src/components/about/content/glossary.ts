/**
 * The Glossary: the app's real user manual, not an afterthought.
 *
 * Transcribed from the NiceGUI About page by parsing its source, not by
 * retyping it — a typo in a hand copy is a wrong definition that nobody would
 * notice. Every term and every word of every definition is the original.
 *
 * Data, not components. `frontend-conventions` §9 asks for a term to be added
 * here when a tab introduces new vocabulary, which is a one-line edit to this
 * file rather than a change to a rendering component.
 */

export interface GlossaryTerm {
  term: string;
  definition: string;
}

export interface GlossarySection {
  title: string;
  terms: GlossaryTerm[];
}

export const GLOSSARY: GlossarySection[] = [
  {
    title: "Order & Risk Basics",
    terms: [
      { term: "SL \u2014 Stop Loss", definition: "The price at which a losing trade closes automatically to cap the loss. Every trade has one." },
      { term: "TP \u2014 Take Profit", definition: "A target price at which some or all of a trade closes for profit. Trades can have up to 8 TP levels (TP1\u2013TP8), closed in stages." },
      { term: "R:R \u2014 Risk:Reward", definition: "How many multiples of the risk taken a trade actually returned. Shown as e.g. \"2.5:1\" or \"+2.5R\" \u2014 a trade risking $50 that banked $125 is 2.5R. Negative means it lost money relative to what was risked." },
      { term: "Realized R", definition: "The R:R actually achieved by a closed trade (net P&L \u00f7 dollar risk taken at entry), as opposed to a plan ratio computed before the trade happened." },
      { term: "BE \u2014 Breakeven", definition: "Moving the Stop Loss to the entry price once a trade is in enough profit, so the trade can no longer lose money even if price reverses." },
      { term: "Pip / Point", definition: "A unit of price movement. For XAUUSD (gold) in this app, 1 point = $0.01 of price movement; a $10.00 move is 1,000 points." },
      { term: "Lot", definition: "The trade size. 0.10 lots on XAUUSD means $10 profit or loss per 1-point price move (1 lot = 100 oz)." },
      { term: "Spread", definition: "The gap between the buy (ask) and sell (bid) price at any moment \u2014 an implicit cost paid on every trade." },
      { term: "Drawdown", definition: "How far equity has fallen from its most recent peak, shown as a percentage. Max Drawdown is the worst such dip over the period shown." },
      { term: "Win Rate", definition: "The percentage of closed trades that ended in profit." },
      { term: "Profit Factor", definition: "Total profit from winning trades divided by total loss from losing trades. Above 1.0 means profitable overall; below 1.0 means losing overall." },
    ],
  },
  {
    title: "Order Types",
    terms: [
      { term: "Market Order", definition: "An order that fills immediately at whatever the current price is." },
      { term: "Limit Order (Pending Order)", definition: "An order that rests unfilled on the broker's book until price reaches a specified level, then fills automatically. Used by the Limit Runner strategy." },
      { term: "GTC \u2014 Good Till Cancelled", definition: "A pending order that stays resting until it either fills or is explicitly cancelled/expires \u2014 as opposed to expiring at the end of the trading day." },
      { term: "Entry Realignment", definition: "Limit Runner setting: if price has already moved through the signalled zone by the time the order would be placed, enters at market instead and shifts SL/TP by the same distance rather than losing the signal to a broker rejection." },
    ],
  },
  {
    title: "Technical Indicators",
    terms: [
      { term: "ADX \u2014 Average Directional Index", definition: "Measures how strong a trend is (not its direction). Above ~25 typically indicates a trending market; below suggests a ranging/choppy one. Several strategies require a minimum ADX before entering." },
      { term: "EMA \u2014 Exponential Moving Average", definition: "A trend line that weights recent price more heavily than older price. This app shows EMA 9/21/50 on the chart." },
      { term: "RSI \u2014 Relative Strength Index", definition: "A 0\u2013100 momentum indicator. Above 70 is typically considered overbought, below 30 oversold." },
      { term: "ATR \u2014 Average True Range", definition: "A measure of typical price movement size over recent candles, used to size stops and detect unusually quiet ('collapsed') markets." },
      { term: "HTF Bias \u2014 Higher-Timeframe Bias", definition: "The prevailing trend direction on a longer timeframe (e.g. H4/H1) than the one a signal fires on, used as a filter or confidence signal." },
    ],
  },
  {
    title: "Automation & Risk Controls",
    terms: [
      { term: "DPM \u2014 Dynamic Position Management", definition: "Replaces fixed-TP management with continuous live monitoring: trails the stop as price moves favourably, banks partial profit at key levels, and can close early on a reversal beyond a configured threshold." },
      { term: "IME \u2014 Immediate Market Entry", definition: "Reads channels for bare 'Buy Now'/'Sell Now' messages and enters at current market price immediately, updating SL/TP automatically when the full signal follows." },
      { term: "Auto-Execution", definition: "When on, incoming Telegram signals are traded automatically. When off, they're recorded but require manual execution." },
      { term: "Circuit Breaker", definition: "An automatic trading pause triggered after a run of consecutive losses, to stop a losing streak from compounding until manually reset or the cooldown elapses." },
      { term: "Kelly Criterion Sizing", definition: "Adjusts live lot size using a fraction (half-Kelly) of the mathematically optimal bet size, based on rolling win rate and R:R. Clamped to a modest \u00b125% adjustment." },
      { term: "Trading Schedule", definition: "A per-day, per-time-window profit target \u2014 once a window's target is hit, no further automated entries fire in that window for the rest of the day." },
      { term: "Signal", definition: "A parsed trade idea (direction, entry, SL, TPs) from a Telegram channel or generated internally \u2014 not yet a trade until it executes." },
      { term: "Channel Strategy", definition: "Which management strategy a given Telegram channel's signals use \u2014 set per channel, or left on Auto for Claude to recommend one based on that channel's own track record." },
    ],
  },
  {
    title: "EA Template Terms",
    terms: [
      { term: "EA \u2014 Expert Advisor", definition: "The native MetaTrader 5 program (ForexTraderBridge.mq5) that can manage a trade directly inside MT5 once handed off from this app, so management continues even if this app briefly disconnects." },
      { term: "EA Template", definition: "A saved, reusable set of entry/management rules (Anchor, Trail, Grid, Stealth, Breakeven, Harvest settings) that fully replaces a channel's normal strategy \u2014 the EA runs it natively." },
      { term: "Anchor", definition: "The reference price a template's grid or trail measures from \u2014 typically the price at signal time." },
      { term: "Trail", definition: "A stop-loss that follows price at a fixed or rule-based distance as it moves favourably, locking in gains without a fixed target." },
      { term: "Grid", definition: "A template mode that places several resting limit orders staggered at fixed intervals, averaging into a position as price moves through each level." },
      { term: "Stealth", definition: "A template mode that delays or disguises order placement/management to reduce visible footprint." },
      { term: "Cancel Pending", definition: "Grid template setting: once one leg of the grid fills, automatically cancel every other still-resting leg instead of letting them all fill." },
      { term: "Harvest", definition: "A template setting that locks in (closes) profit once it crosses a configured threshold, independent of the normal TP ladder." },
    ],
  },
  {
    title: "Strategy Names",
    terms: [
      { term: "Scale Out", definition: "Closes a portion of the position at each TP level and moves SL to breakeven after TP1." },
      { term: "BE Runner", definition: "Keeps the full position open with no partial closes; SL steps forward to each newly-cleared TP price." },
      { term: "Trailing Stop", definition: "SL trails a fixed distance behind price once TP1 is reached, with no fixed final target." },
      { term: "Protected Scale", definition: "Holds through TP1 and TP2 (SL moves to breakeven at TP2), then scales out from TP3 onward." },
      { term: "Conservative", definition: "Ignores the signal's own SL/TP entirely and uses a fixed, tight 5-point SL / 3-point TP1 from the actual fill price." },
      { term: "Conservative Trial", definition: "A variant of Conservative with its own fixed SL/TP/breakeven schedule, used to trial different fixed levels without touching the original Conservative strategy." },
      { term: "Scalp Runner", definition: "A tight two-stage scalp: an initial small target confirms the move, then the remaining position trails on a close stop." },
      { term: "Signal Climber", definition: "Uses the signal's own full TP ladder (up to TP8), closing a share at each level and stepping SL forward as each one clears." },
      { term: "Reversal Runner", definition: "The Reversal Engine's own management style \u2014 widens the signal's stop within limits and rides the full TP ladder." },
      { term: "Adaptive Runner / Adaptive Runner 2", definition: "Variants of Reversal Runner with different stop-widening rules (capped proportionate to reward, or a flat fixed distance) \u2014 built for signal sources with inconsistent TP-ladder quality." },
      { term: "Limit Runner", definition: "Places a genuine resting limit order at the broker rather than waiting to fill at market, then manages the position with its own TP ladder once filled." },
    ],
  },
];
