"""What the model is told, and what it is required to answer with.

The model is a REVIEWER here, not the strategist. The candidate trade has
already been built from the rules by the time this prompt is written, and the
evidence it was built from is handed over with it. The model's job is to say
whether the read holds up, to tighten the levels if the structure justifies it,
and to name what would invalidate it.

That framing is deliberate. Asked to find a trade, a model finds one every
time, on every chart, including the ones with nothing on them. Asked to judge a
specific proposal against stated rules, it has something to disagree with --
and a "skip" from it is worth more than an entry.

Nothing it returns is trusted. `analysis.evaluate` re-validates every level it
sends back through `setup.invalidations` and discards what fails.
"""
from __future__ import annotations

SYSTEM = (
    "You are reviewing a swing-trade setup on gold (XAUUSD) against the "
    "Set & Forget method taught by Alex G (fxalexg / Swing Trading Lab). "
    "You are not looking for a trade: one has been proposed to you from the "
    "rules, and you are judging it.\n\n"
    "The method's rules, which you hold the proposal to:\n"
    "1. Top-down: Weekly for bias, Daily for structure, 4H for the entry. If "
    "the Weekly and Daily disagree the pair is too noisy and there is no "
    "trade.\n"
    "2. Entries happen only at an Area of Interest -- a former swing point or "
    "consolidation acting as supply or demand -- in the direction of the "
    "higher-timeframe bias. Never mid-range.\n"
    "3. The stop is structural: beyond the zone, or beyond the tail of the "
    "confirmation candle. Never a round number of points.\n"
    "4. The target is the next opposing Area of Interest, not a multiple of "
    "the risk.\n"
    "5. Minimum 1:2 reward-to-risk. Under that, there is no trade, however "
    "good the chart looks.\n"
    "6. The order rests at the zone as a limit unless price is already there. "
    "Chasing price away from a zone is not this method.\n\n"
    "Answer with a single JSON object and nothing else -- no prose around it, "
    "no code fence. Keys:\n"
    '  "verdict": "take" | "adjust" | "skip"\n'
    '  "entry", "stop_loss", "take_profit": numbers (omit them for a skip)\n'
    '  "order_type": "market" | "limit"\n'
    '  "reasoning": 2-4 sentences, plain English, naming the levels you used\n'
    '  "risks": one sentence on what would invalidate this, or "" if nothing '
    "specific\n\n"
    "Use \"adjust\" when the trade is sound but a level should move, and say "
    "in reasoning which and why. Use \"skip\" freely: a setup that does not "
    "meet the rules is the common case, and saying so is the answer. Never "
    "invent a level to make the ratio work."
)

# Enough for the JSON plus a short piece of reasoning. Generous rather than
# tight: a reply truncated at max_tokens is unparseable, and the provider
# raises TruncatedResponseError rather than handing back half an object.
MAX_TOKENS = 1200


def _zone_lines(zones: list[dict], price: float) -> list[str]:
    if not zones:
        return ["  (none found)"]
    out = []
    for z in sorted(zones, key=lambda z: z["low"], reverse=True):
        where = "above" if z["low"] > price else "below"
        tested = f", tested {z['touches']}x" if z.get("touches", 1) > 1 else ""
        out.append(f"  {z['kind']:<7} {z['low']:.2f} – {z['high']:.2f}  "
                   f"({where} price{tested})")
    return out


def render(evidence: dict, candidate: dict) -> str:
    """The evidence and the proposal, as the model reads them.

    Everything here was measured, not inferred. The model is given no candle
    series: it would re-derive the structure from it, disagree with the numbers
    printed beside it on screen, and there would be no way to tell which of the
    two the operator was looking at.
    """
    price = evidence.get("price")
    lines: list[str] = [
        "GOLD (XAUUSD) — measured evidence",
        f"Current price: {price:.2f}" if price else "Current price: unavailable",
        "",
        "Top-down read:",
        f"  Weekly structure: {evidence.get('weekly_bias')}",
        f"  Daily structure:  {evidence.get('daily_bias')}",
        f"  {evidence.get('entry_timeframe', '4H')} structure:     "
        f"{evidence.get('entry_bias')}",
        "",
        "Areas of interest:",
        *_zone_lines(evidence.get("zones") or [], price or 0.0),
        "",
        "Entry-timeframe indicators:",
        f"  EMA 50:  {evidence.get('ema_fast')}",
        f"  EMA 200: {evidence.get('ema_slow')}",
        f"  RSI 14:  {evidence.get('rsi')}",
        f"  ATR 14:  {evidence.get('atr')}",
    ]

    fib = evidence.get("fib")
    if fib is not None:
        lines.append(f"  Pullback into the last leg: {fib * 100:.1f}%")
    found = evidence.get("confirmation")
    lines.append(
        f"  Last closed bar: {found['direction']} "
        f"{str(found['kind']).replace('_', ' ')}" if found
        else "  Last closed bar: no confirmation pattern"
    )

    lines += [
        "",
        "Proposed from the rules:",
        f"  {candidate['direction']} as a {candidate['order_type']} order",
        f"  Entry:  {candidate['entry']:.2f}",
        f"  Stop:   {candidate['stop_loss']:.2f}",
        f"  Target: {candidate['take_profit']:.2f}",
        f"  Reward-to-risk: 1:{candidate['rr']:.2f}"
        if candidate.get("rr") else "  Reward-to-risk: unmeasurable",
        "",
        "Judge this proposal against the rules. Answer with the JSON object "
        "only.",
    ]
    return "\n".join(lines)
