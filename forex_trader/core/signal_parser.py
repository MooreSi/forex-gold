"""
Gold signal parsers — extracted verbatim from the original service.py.
Supports two Telegram channel formats.
"""

import json
import logging
import re
from typing import Optional

_log = logging.getLogger(__name__)

# Currency guard — capture a wider token (allows "XAU/USD", "XAU-USD") and
# strip separators before comparing, so a slash/dash in the ticker doesn't
# truncate the captured group to just "XAU" and wrongly reject a real
# XAUUSD signal.
_CURRENCY_RE = re.compile(r'Currency[\s\xa0]*:[\s\xa0]*([\w/\-]+)', re.IGNORECASE)

# Format A: "Sell Gold 4520 - 4512"
_DIRECTION_RE   = re.compile(r'\b(buy|sell)\s+gold\s+([\d.,]+)\s*[-–—]\s*([\d.,]+)', re.IGNORECASE)
# Format B: "Direction  SELL" + "ENTRY : 4468-4466" — colon after "Direction"
# is optional since GD VIP has sent it both with and without one.
_DIRECTION_B_RE = re.compile(r'Direction[\s\xa0]*:?[\s\xa0]+(BUY|SELL)', re.IGNORECASE)
# Dash/en-dash/em-dash/slash or the word "to" between the two entry prices.
_RANGE_SEP = r'[\s\xa0]*(?:[-–—/]|to)[\s\xa0]*'
_ENTRY_B_RE     = re.compile(r'ENTRY[\s\xa0]*:?[\s\xa0]*([\d.,]+)' + _RANGE_SEP + r'([\d.,]+)', re.IGNORECASE)
# Stop loss — colon after "Stop Loss" (full words) is optional, matching the
# same optional-colon tolerance as Direction/ENTRY above.
_SL_RE   = re.compile(r'stop[\s\xa0]*loss[\s\xa0]*:?[\s\xa0]+([\d.,]+)', re.IGNORECASE)
_SL_B_RE = re.compile(r'\bSL[\s\xa0]*:?[\s\xa0]*([\d.,]+)', re.IGNORECASE)
# TPs — colon or dash before the number, both optional.
_TP_RE     = re.compile(r'TP(\d+)[\s\xa0]+([\d.,]+)', re.IGNORECASE)
_TP_B_RE   = re.compile(r'TP(\d+)[\s\xa0]*[:\-][\s\xa0]*([\d.,]+)', re.IGNORECASE)
_TPOPEN_RE = re.compile(r'TP(\d)\s+Open\s*\(([\d.,/\s]+)\)', re.IGNORECASE)

# Format C: Gold Diggers 2.0 — "XAU USD BUY NOW" / "XAUUSD BUY NOW"
# "NOW" is optional: GD2 sometimes omits it in the initial message.
_GD2_DIRECTION_RE    = re.compile(r'XAU\s*USD\s+(BUY|SELL)(?:\s+NOW)?\b', re.IGNORECASE)
_GD2_ENTRY_RANGE_RE  = re.compile(r'([\d.,]+)\s*[-–—]\s*([\d.,]+)')
_GD2_ENTRY_SINGLE_RE = re.compile(r'\b(\d[\d.,]*)\b')  # fallback: first price-like number
_GD2_TP_RE           = re.compile(r'TP(\d+)[\s\xa0]*:?[\s\xa0]+([\d.,]+)', re.IGNORECASE)
_GD2_SL_RE           = re.compile(r'(?:🚫\s*)?SL[\s\xa0]*:?[\s\xa0]+([\d.,]+)', re.IGNORECASE)

# Format C2: Gold Diggers 2.0's newer "Zone"/"Gold" layout (introduced
# 2026-07-06 as "Zone"; "Gold" wording confirmed live 2026-07-08 from the
# "GOLD DIGGERS 2.0 ⚡️" channel — same structure, different trigger noun,
# missed several signals before this was added since neither variant matched
# the older XAU USD gate) —
#   "Buy Zone Now" / "Sell Zone Now"  (or "Buy Gold Now" / "Sell Gold Now")
#   4163.5 - 4158.5
#   Targets
#   4165.5
#   4167.5
#   4170
#   SL/ invalid 4155.5
# TPs are bare numbers, one per line, under a "Targets" header — no "TP1:"
# prefix like the older format — and SL uses "SL/ invalid <price>" instead
# of "SL: <price>". Coexists with the older XAU USD BUY/SELL format; both
# are tried by parse_gd2_signal/parse_gd2_partial so channel format changes
# don't need call-site changes anywhere else.
#   Target        <- singular header seen live too, alongside "Targets"
#   4063
#   4065
#   4067
# "Now" is required for the "Gold" noun (never optional, unlike "Zone") since
# Format A's own gate is literally "buy/sell gold <price> - <price>"
# (_DIRECTION_RE above) — without requiring "Now" here, "Sell Gold 4520 -
# 4512" would incorrectly satisfy this gate too for any 'auto'-format channel.
# "Buy/Sell Zone" alone (no "Now") also matches here via .search() even when
# preceded by other words -- e.g. "Next Buy Zone" (Gold Diggers Scalping,
# added 2026-07-24) -- since \b(Buy|Sell) only anchors the start of that
# word, not the start of the message.
_GD2_ZONE_DIRECTION_RE = re.compile(r'\b(Buy|Sell)\s+(?:Zone(?:\s+Now)?|Gold\s+Now)\b', re.IGNORECASE)
_GD2_TARGETS_HEADER_RE = re.compile(r'Targets?\b', re.IGNORECASE)
_GD2_TARGET_LINE_RE    = re.compile(r'^([\d.,]+)$')
# SL/ invalid <price>" (original GD2 wording) or plain "SL <price>"/"SL: <price>"
# (Gold Diggers Scalping's "Next Buy Zone" layout, added 2026-07-24 -- was
# previously only matched by _GD2_SL_RE, which this is_zone branch never
# tried, so these messages fell through to parse_gd2_partial forever instead
# of ever completing as a real signal).
_GD2_SL_ZONE_RE        = re.compile(
    r'SL[\s\xa0]*(?:/[\s\xa0]*invalid)?[\s\xa0]*:?[\s\xa0]+([\d.,]+)', re.IGNORECASE
)

# Format C3: Gold Diggers 2.0's "BUY [LIMITS] GOLD @ xxxx/yyyy AREA" layout
# (confirmed live 2026-07-09 — multiple signals using this wording).
#   "BUY LIMITS GOLD @ 4073/4067 AREA"  or  "BUY GOLD @ 4108/4103"
#   TP 4076                   ← bare "TP <price>", no TP1/TP2 numbering
#   TP 4080
#   TP 4085
#   TP OPEN                   ← filler — skipped (not a number)
#   SL 4066
# Entry range is slash-separated (high/low for BUY, low/high for SELL).
# TPs use a "TP <price>" prefix (no ordinal number). SL uses bare "SL <price>".
# "LIMITS" is optional — "BUY GOLD @" also appears.
# Requires "@" after GOLD to avoid conflicting with Format A ("Sell Gold 4520 - 4512").
_GD2_LIMITS_DIRECTION_RE = re.compile(r'\b(BUY|SELL)\s+(?:LIMITS?\s+)?GOLD\s+@', re.IGNORECASE)
_GD2_AT_RANGE_RE         = re.compile(r'@\s*([\d.,]+)\s*/\s*([\d.,]+)')
_GD2_TP_PLAIN_RE         = re.compile(r'^TP\s+([\d.,]+)\s*$', re.IGNORECASE | re.MULTILINE)
# A literal "TP OPEN" line — the final, unfixed-target leg that rides as a
# runner once every numeric TP above it has closed its portion (see
# core_limit_order_signal.py). Distinct from _GD2_TP_PLAIN_RE, which only
# matches a numeric value and therefore never matches this line at all.
_GD2_TP_OPEN_RE          = re.compile(r'^TP\s+OPEN\s*$', re.IGNORECASE | re.MULTILINE)


def _parse_gd2_zone_targets(text: str, start: int = 0) -> dict[int, float]:
    """Extract sequential TPs from a "Targets" section: one bare number per
    line, in order, until the SL line. Non-numeric noise lines (blank, or
    stray words like "Open" seen in some live messages) are skipped rather
    than treated as a stop condition, since GD2's own formatting is
    inconsistent about what appears between the last target and the SL."""
    tps: dict[int, float] = {}
    hm = _GD2_TARGETS_HEADER_RE.search(text, start)
    if not hm:
        return tps
    n = 0
    for line in text[hm.end():].splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if re.search(r'\bSL\b', stripped, re.IGNORECASE):
            break
        lm = _GD2_TARGET_LINE_RE.match(stripped)
        if lm:
            n += 1
            if n <= 8:
                tps[n] = _f(lm.group(1))
        if n >= 8:
            break
    return tps


def _parse_gd2_plain_tps(text: str) -> dict[int, float]:
    """Extract sequential TPs from the Format C3 'TP <price>' layout.
    Each line is 'TP 4076', 'TP 4080', etc. Lines like 'TP OPEN' are ignored
    since _GD2_TP_PLAIN_RE only matches a numeric value after the TP prefix.
    Returns {1: price1, 2: price2, ...} in match order."""
    tps: dict[int, float] = {}
    n = 0
    for m in _GD2_TP_PLAIN_RE.finditer(text):
        n += 1
        if n <= 8:
            tps[n] = _f(m.group(1))
        if n >= 8:
            break
    return tps

# Instant entry — "XAUUSD Buy Now" / "XAU Sell Now" / "XAU USD BUY NOW"
# optionally followed by a limit price, e.g. "XAUUSD Buy Now 4293"
# without SL/TP details; covered by Immediate Market Buy/Sell feature.
_INSTANT_RE = re.compile(
    r'\b(?:XAUUSD|XAU\s*USD|XAU)\s+(BUY|SELL)\s+NOW\b(?:[\s@,]*([\d.,]+))?',
    re.IGNORECASE,
)

# GD2 instant entry — "XAU USD BUY [NOW]" / "XAU USD SELL [NOW]"
# "NOW" is optional: GD2 sometimes sends "XAU USD BUY" without it.
_GD2_IME_RE = re.compile(
    r'\bXAU\s*USD\s+(BUY|SELL)(?:\s+NOW)?\b',
    re.IGNORECASE,
)

# Prefix that Channel 1 (Gold Diggers VIP) signals start with
SIGNAL_PREFIX = "This is not financial advice. Use appropriate risk management if you're going to trade."


def is_format_ab_signal(text: str, prefix: Optional[str] = None) -> bool:
    """True if text plausibly is a Format A/B (Gold Diggers VIP) signal.

    Previously gated on a strict, case-sensitive text.startswith(prefix) at
    position 0 — a bolded disclaimer, a leading emoji, or any other
    incidental formatting drift ahead of the disclaimer defeated that check
    silently (no log, no "unrecognised" queue entry — the message just
    vanished with no trace). This looks for the prefix as a case-insensitive
    substring near the start of the message instead (tolerant of markdown
    bold wrapping and a few leading characters), and falls back to the
    signal's own structural markers (Direction + ENTRY, or "buy/sell gold")
    when the disclaimer wording itself doesn't match at all — so a channel
    tweaking its disclaimer text doesn't silently stop being read.
    """
    if not text:
        return False
    head = re.sub(r'\*+', '', text[:200])
    key = (prefix or SIGNAL_PREFIX)[:40].lower()
    if key in head.lower():
        return True
    return bool(_DIRECTION_B_RE.search(text) and _ENTRY_B_RE.search(text)) or \
        bool(_DIRECTION_RE.search(text))


def _f(s: str) -> float:
    return float(str(s).replace(",", "").strip())


def _autocorrect_tps(direction: str, entry_low: float, entry_high: float,
                     raw: dict) -> dict:
    """
    Detect and fix malformed TP levels in a freshly parsed signal dict.

    Three classes of problem corrected:
    1. TPs on the wrong side of the entry zone (below entry for BUY, above for
       SELL) — dropped.
    2. TPs out of monotonic order — re-sorted into the correct direction.
    3. Gaps left after dropping invalid TPs — extrapolated forward using the
       step between the last two valid TPs.

    Returns the (possibly modified) dict.  Logs a WARNING for every channel
    that sent a malformed signal so the correction is visible in the app log.
    """
    is_buy   = direction.upper() == "BUY"
    ref      = entry_high if is_buy else entry_low
    keys     = [f"tp{i}" for i in range(1, 9)]
    provided = [(k, raw[k]) for k in keys if raw.get(k) is not None]

    if not provided:
        return raw

    fixes: list[str] = []

    # ── 1. Drop TPs on the wrong side of entry ────────────────────────────────
    valid: list[float] = []
    for k, v in provided:
        if (is_buy and v <= ref) or (not is_buy and v >= ref):
            fixes.append(f"{k}={v:.2f} dropped (wrong side of entry {ref:.2f})")
        else:
            valid.append(v)

    if not valid:
        if fixes:
            _log.warning("[SignalParser] TP autocorrect: all TPs invalid — %s", "; ".join(fixes))
        return raw  # nothing salvageable; leave as-is so validate_signal surfaces the error

    # ── 2. Sort + deduplicate ─────────────────────────────────────────────────
    seen: set[float] = set()
    ordered: list[float] = []
    for v in sorted(valid, reverse=(not is_buy)):
        if v not in seen:
            seen.add(v)
            ordered.append(v)

    original = [v for _, v in provided]
    if ordered != original:
        fixes.append(
            f"reordered {[f'{v:.2f}' for v in original]} "
            f"-> {[f'{v:.2f}' for v in ordered]}"
        )

    # ── 3. Extrapolate to restore the original TP count ───────────────────────
    target = len(provided)
    while len(ordered) < target and len(ordered) >= 2:
        step    = ordered[-1] - ordered[-2]
        new_val = round(ordered[-1] + step, 2)
        slot    = f"tp{len(ordered) + 1}"
        fixes.append(f"{slot} extrapolated as {new_val:.2f} (step {step:+.2f})")
        ordered.append(new_val)

    if fixes:
        _log.warning(
            "[SignalParser] TP autocorrect on %s signal — %s",
            direction, "; ".join(fixes),
        )

    # ── 4. Write corrected values back ────────────────────────────────────────
    corrected = dict(raw)
    for i, k in enumerate(keys):
        corrected[k] = ordered[i] if i < len(ordered) else None
    return corrected


def parse_instant_entry(text: str) -> Optional[tuple]:
    """
    Detect an instant-entry message such as 'XAUUSD Buy Now' or 'XAU Sell Now 4293'.
    Returns (direction, price) where price is a float or None (market).

    Returns None if the message also contains SL or TP data — that makes it a full
    signal that should go through the normal parser, not the instant-entry path.
    """
    m = _INSTANT_RE.search(text)
    if not m:
        return None
    # If the message carries SL or TP values it is a full signal, not instant
    if re.search(r'\bSL[\s:]+[\d.,]+', text, re.IGNORECASE):
        return None
    if re.search(r'\bTP\s*\d', text, re.IGNORECASE):
        return None
    price_str = m.group(2)
    price = _f(price_str) if price_str else None
    return (m.group(1).upper(), price)


def parse_gd2_instant_entry(text: str) -> Optional[tuple]:
    """
    Detect a GD2 instant-entry (bare direction, nothing else yet) message:
    'XAU USD BUY [NOW]' / 'XAU USD SELL [NOW]', or the newer "Buy/Sell Zone
    Now" trigger (see _GD2_ZONE_DIRECTION_RE) — GD2 changed formats
    2026-07-06 and this was missed in that pass, silently disabling
    Immediate Market Entry for the new format's bare trigger even though the
    setting was still on and correctly synced between nodes.
    Matches with or without 'NOW'.  Always returns a market order (price=None).

    Returns None if the message already contains SL or TP data (in either
    format's own syntax) — that is a full signal and should go through
    parse_gd2_signal instead.
    """
    m = _GD2_DIRECTION_RE.search(text)
    is_zone = False
    if not m:
        m = _GD2_ZONE_DIRECTION_RE.search(text)
        is_zone = True
    if not m:
        return None

    if is_zone:
        if _GD2_SL_ZONE_RE.search(text):
            return None
        if _GD2_TARGETS_HEADER_RE.search(text, m.end()):
            return None
    else:
        if re.search(r'\bSL[\s:]+[\d.,]+', text, re.IGNORECASE):
            return None
        if re.search(r'\bTP\s*\d', text, re.IGNORECASE):
            return None

    # If a price range follows the direction keyword, this is a partial signal
    # (direction + entry range, SL/TP to follow via edit).  Let parse_gd2_partial
    # handle it so the range is captured and the signal awaits the followup edit.
    if _GD2_ENTRY_RANGE_RE.search(text, m.end()):
        return None
    return (m.group(1).upper(), None)


def parse_gold_signal(text: str) -> Optional[dict]:
    """
    Parse a Format A/B (Gold Diggers VIP) XAUUSD signal message.
    Returns a dict of signal fields or None if not a valid signal.
    """
    currency_m = _CURRENCY_RE.search(text)
    if currency_m and currency_m.group(1).upper().replace("/", "").replace("-", "") != "XAUUSD":
        return None

    clean = re.sub(r'\*+([^*\n]+)\*+', r'\1', text)

    direction = entry_low = entry_high = stop_loss = None

    m = _DIRECTION_RE.search(clean)
    if m:
        direction  = m.group(1).upper()
        price_a    = _f(m.group(2))
        price_b    = _f(m.group(3))
        entry_low  = min(price_a, price_b)
        entry_high = max(price_a, price_b)

    if direction is None:
        mb = _DIRECTION_B_RE.search(clean)
        eb = _ENTRY_B_RE.search(clean)
        if mb and eb:
            direction  = mb.group(1).upper()
            price_a    = _f(eb.group(1))
            price_b    = _f(eb.group(2))
            entry_low  = min(price_a, price_b)
            entry_high = max(price_a, price_b)

    if direction is None:
        return None

    sl_m = _SL_RE.search(clean) or _SL_B_RE.search(clean)
    if not sl_m:
        return None
    stop_loss = _f(sl_m.group(1))

    tps: dict[int, float] = {}
    for tm in _TP_RE.finditer(clean):
        n = int(tm.group(1))
        if n <= 9:
            tps.setdefault(n, _f(tm.group(2)))
    for tm in _TP_B_RE.finditer(clean):
        n = int(tm.group(1))
        if n <= 9:
            tps[n] = _f(tm.group(2))

    om = _TPOPEN_RE.search(clean)
    if om:
        open_tp_num = int(om.group(1))
        values = [_f(v) for v in re.split(r'[/\s]+', om.group(2).strip()) if v.strip()]
        for i, v in enumerate(values[:2]):
            slot = open_tp_num + i
            if slot <= 5:
                tps.setdefault(slot, v)

    raw = {
        "direction":  direction,
        "entry_low":  entry_low,
        "entry_high": entry_high,
        "stop_loss":  stop_loss,
        "tp1": tps.get(1),
        "tp2": tps.get(2),
        "tp3": tps.get(3),
        "tp4": tps.get(4),
        "tp5": tps.get(5),
        "tp6": tps.get(6),
        "tp7": tps.get(7),
        "tp8": tps.get(8),
    }
    return _autocorrect_tps(direction, entry_low, entry_high, raw)


def parse_gd2_signal(text: str) -> Optional[dict]:
    """
    Parse a Format C (Gold Diggers 2.0) XAUUSD signal.
    Three layout variants, tried in order:

    C1 — "XAU USD BUY/SELL NOW" (older):
         entry range is dash-separated; TPs use "TP1: <price>" labels.
    C2 — "Buy/Sell Zone Now" / "Buy/Sell Gold Now" (added 2026-07-06):
         entry range is dash-separated below the direction line;
         TPs are bare numbers under a "Targets" header;
         SL uses "SL/ invalid <price>".
    C3 — "BUY [LIMITS] GOLD @ xxxx/yyyy AREA" (observed 2026-07-09):
         entry range is slash-separated after "@";
         TPs use bare "TP <price>" lines (no ordinal, "TP OPEN" ignored);
         SL uses bare "SL <price>".
    """
    # ── C1: XAU USD BUY/SELL [NOW] ──────────────────────────────────────────
    dm = _GD2_DIRECTION_RE.search(text)
    is_zone = False
    is_limits = False
    if not dm:
        # ── C2: Buy/Sell Zone/Gold Now ───────────────────────────────────────
        dm = _GD2_ZONE_DIRECTION_RE.search(text)
        if dm:
            is_zone = True
    if not dm:
        # ── C3: BUY [LIMITS] GOLD @ xxxx/yyyy ───────────────────────────────
        dm = _GD2_LIMITS_DIRECTION_RE.search(text)
        if dm:
            is_limits = True
    if not dm:
        return None
    direction = dm.group(1).upper()

    if is_limits:
        # Entry range is "@ high/low AREA" (slash-separated)
        arm = _GD2_AT_RANGE_RE.search(text)
        if not arm:
            return None
        price_a   = _f(arm.group(1))
        price_b   = _f(arm.group(2))
        entry_low  = min(price_a, price_b)
        entry_high = max(price_a, price_b)
    else:
        em = _GD2_ENTRY_RANGE_RE.search(text, dm.end())
        if em:
            price_a    = _f(em.group(1))
            price_b    = _f(em.group(2))
            entry_low  = min(price_a, price_b)
            entry_high = max(price_a, price_b)
        else:
            sm = _GD2_ENTRY_SINGLE_RE.search(text, dm.end())
            if not sm:
                return None
            price      = _f(sm.group(1))
            entry_low  = entry_high = price

    if is_zone:
        tps = _parse_gd2_zone_targets(text, dm.end())
    elif is_limits:
        # C3: bare "TP <price>" lines; "TP OPEN" skipped by regex
        tps = _parse_gd2_plain_tps(text)
    else:
        tps = {}
        for tm in _GD2_TP_RE.finditer(text):
            n = int(tm.group(1))
            if 1 <= n <= 8:
                tps[n] = _f(tm.group(2))
    if 1 not in tps:
        return None

    slm = (_GD2_SL_ZONE_RE if is_zone else _GD2_SL_RE).search(text)
    if not slm:
        return None
    stop_loss = _f(slm.group(1))

    raw = {
        "direction":  direction,
        "entry_low":  entry_low,
        "entry_high": entry_high,
        "stop_loss":  stop_loss,
        "tp1": tps.get(1),
        "tp2": tps.get(2),
        "tp3": tps.get(3),
        "tp4": tps.get(4),
        "tp5": tps.get(5),
        "tp6": tps.get(6),
        "tp7": tps.get(7),
        "tp8": tps.get(8),
    }
    return _autocorrect_tps(direction, entry_low, entry_high, raw)


def parse_gd2_partial(text: str) -> Optional[dict]:
    """
    Detect a GD2 message that has direction + entry range but is missing SL and/or TP.
    GD2 sometimes sends "XAU USD SELL NOW\n4150.5 - 4155.5" first, then edits to add
    SL/TP later.  This parser records those early messages so the edit can complete them.
    Covers all three GD2 variants (C1/C2/C3 — see parse_gd2_signal for layout details).

    Returns {"direction", "entry_low", "entry_high"} or None.
    Returns None if the message already has both SL and TP (use parse_gd2_signal instead).
    """
    dm = _GD2_DIRECTION_RE.search(text)
    is_zone = False
    is_limits = False
    if not dm:
        dm = _GD2_ZONE_DIRECTION_RE.search(text)
        if dm:
            is_zone = True
    if not dm:
        dm = _GD2_LIMITS_DIRECTION_RE.search(text)
        if dm:
            is_limits = True
    if not dm:
        return None
    direction = dm.group(1).upper()

    if is_limits:
        arm = _GD2_AT_RANGE_RE.search(text)
        if not arm:
            return None
        price_a    = _f(arm.group(1))
        price_b    = _f(arm.group(2))
        entry_low  = min(price_a, price_b)
        entry_high = max(price_a, price_b)
    else:
        em = _GD2_ENTRY_RANGE_RE.search(text, dm.end())
        if em:
            price_a    = _f(em.group(1))
            price_b    = _f(em.group(2))
            entry_low  = min(price_a, price_b)
            entry_high = max(price_a, price_b)
        else:
            sm = _GD2_ENTRY_SINGLE_RE.search(text, dm.end())
            if not sm:
                return None
            price      = _f(sm.group(1))
            entry_low  = entry_high = price

    # If both SL and at least one TP are present, this is a full signal —
    # let parse_gd2_signal handle it.
    if is_zone:
        if _GD2_SL_ZONE_RE.search(text) and _parse_gd2_zone_targets(text, dm.end()):
            return None
    elif is_limits:
        if _GD2_SL_RE.search(text) and _parse_gd2_plain_tps(text):
            return None
    else:
        if _GD2_SL_RE.search(text) and _GD2_TP_RE.search(text):
            return None

    return {"direction": direction, "entry_low": entry_low, "entry_high": entry_high}


def is_gd2_message(text: str) -> bool:
    """True if this message plausibly came from Gold Diggers 2.0, in any of
    the three known layout variants:
      C1 — "XAU USD BUY/SELL NOW"
      C2 — "Buy/Sell Zone Now" / "Buy/Sell Gold Now"
      C3 — "BUY [LIMITS] GOLD @ xxxx/yyyy AREA" (added 2026-07-09)
    Callers used to gate GD2 handling on a bare `"XAU" in text` check, which
    silently dropped every message in the C2/C3 formats — none of their text
    mentions XAU anywhere. Use this instead."""
    return (
        _GD2_DIRECTION_RE.search(text) is not None or
        _GD2_ZONE_DIRECTION_RE.search(text) is not None or
        _GD2_LIMITS_DIRECTION_RE.search(text) is not None
    )


def is_limit_order_signal(text: str) -> bool:
    """True for the "BUY/SELL [LIMITS] GOLD @ x/y AREA" pending-order layout
    (GD2 Format C3) — checked independently of any channel's configured
    parser_format/is_gd2_message gate, so a genuine broker-side pending
    order can be placed for this exact shape no matter which channel it
    arrives from (any channel, format-matched only)."""
    return _GD2_LIMITS_DIRECTION_RE.search(text) is not None


def parse_limit_order_signal(text: str) -> Optional[dict]:
    """Parse the C3 pending-order layout into a signal dict — same shape as
    parse_gd2_signal() (direction/entry_low/entry_high/stop_loss/tp1..tp8)
    plus a `tp_open` flag: True if the message contains a literal "TP OPEN"
    line, meaning the portion left after the last numeric TP has no fixed
    target and rides as a runner (see core_limit_order_signal.py). Delegates
    the actual field extraction to parse_gd2_signal — this function only
    adds the format gate + the tp_open flag, so C3's parsing logic has a
    single source of truth."""
    if not is_limit_order_signal(text):
        return None
    parsed = parse_gd2_signal(text)
    if not parsed:
        return None
    parsed = dict(parsed)
    parsed["tp_open"] = bool(_GD2_TP_OPEN_RE.search(text))
    return parsed


def parse_format_ab_partial(text: str) -> Optional[dict]:
    """Format A/B counterpart to parse_gd2_partial: direction + entry range
    with no Stop Loss and no TP yet. parse_gold_signal returns None outright
    when _SL_RE/_SL_B_RE don't match, so without this a Format A/B channel
    that splits its entry and its levels across two messages produces no
    signal dict at all -- there was nothing for a follow-up to complete.

    Returns {"direction", "entry_low", "entry_high"} or None. Returns None
    when SL or any TP is already present (that's a full signal -- use
    parse_gold_signal) and for a non-XAUUSD Currency line, matching
    parse_gold_signal's own gate so this can't smuggle in a pair the app
    doesn't trade."""
    currency_m = _CURRENCY_RE.search(text)
    if currency_m and currency_m.group(1).upper().replace("/", "").replace("-", "") != "XAUUSD":
        return None

    clean = re.sub(r'\*+([^*\n]+)\*+', r'\1', text)

    direction = entry_low = entry_high = None
    m = _DIRECTION_RE.search(clean)
    if m:
        direction = m.group(1).upper()
        price_a, price_b = _f(m.group(2)), _f(m.group(3))
        entry_low, entry_high = min(price_a, price_b), max(price_a, price_b)
    else:
        mb, eb = _DIRECTION_B_RE.search(clean), _ENTRY_B_RE.search(clean)
        if mb and eb:
            direction = mb.group(1).upper()
            price_a, price_b = _f(eb.group(1)), _f(eb.group(2))
            entry_low, entry_high = min(price_a, price_b), max(price_a, price_b)

    if direction is None:
        return None
    if _SL_RE.search(clean) or _SL_B_RE.search(clean):
        return None
    if _TP_RE.search(clean) or _TP_B_RE.search(clean):
        return None
    return {"direction": direction, "entry_low": entry_low, "entry_high": entry_high}


def _extract_levels(text: str) -> dict:
    """Every SL/TP this message states, in any supported format's syntax.
    Values absent from the text come back as None."""
    clean = re.sub(r'\*+([^*\n]+)\*+', r'\1', text)

    sl_m = _SL_RE.search(clean) or _SL_B_RE.search(clean) or _GD2_SL_RE.search(clean)
    tps: dict[int, float] = {}
    for pattern in (_TP_RE, _TP_B_RE, _GD2_TP_RE):
        for tm in pattern.finditer(clean):
            n = int(tm.group(1))
            if n <= 8:
                tps.setdefault(n, _f(tm.group(2)))

    return {
        "stop_loss": _f(sl_m.group(1)) if sl_m else None,
        **{f"tp{i}": tps.get(i) for i in range(1, 9)},
    }


def parse_partial_any_format(text: str) -> Optional[dict]:
    """Format-agnostic "direction + entry, levels still to come" detector for
    TP/SL in Second Message. Tries GD2's existing partial parser first (it
    covers all three GD2 layouts), then Format A/B's.

    Carries through any SL/TP the message *did* state: parse_gd2_partial
    treats a message as partial when either SL or TP is missing, so a GD2
    entry that quotes its Stop Loss but no targets yet reaches here with a
    real SL that must not be thrown away -- holding it and later executing
    bare would otherwise place the trade with no stop at all.

    Returns {"direction", "entry_low", "entry_high", "stop_loss",
    "tp1".."tp8"} or None."""
    partial = parse_gd2_partial(text) or parse_format_ab_partial(text)
    if not partial:
        return None
    result = {**partial, **_extract_levels(text)}
    # `tp_open` is what routes a signal to Limit Runner's resting pending
    # order rather than market execution, so a held C3 "[LIMITS] GOLD @"
    # entry has to carry it through the merge or it would come back out as
    # an ordinary market signal.
    if is_limit_order_signal(text):
        result["tp_open"] = bool(_GD2_TP_OPEN_RE.search(text))
    return result


def parse_tp_sl_only(text: str) -> Optional[dict]:
    """The other half of TP/SL in Second Message: a follow-up message that
    carries Stop Loss and/or TP levels but names no direction and no entry
    of its own, so it can only be read as completing an earlier signal.

    Returns {"stop_loss", "tp1".."tp8"} (any of which may be None) or None
    if the message states a direction, or carries neither SL nor any TP.
    Refusing anything with a direction is what keeps this from swallowing a
    complete standalone signal -- that must go through the normal parsers."""
    if (_DIRECTION_RE.search(text) or _DIRECTION_B_RE.search(text)
            or _GD2_DIRECTION_RE.search(text) or _GD2_ZONE_DIRECTION_RE.search(text)
            or _GD2_LIMITS_DIRECTION_RE.search(text)):
        return None

    levels = _extract_levels(text)
    if levels["stop_loss"] is None and all(levels[f"tp{i}"] is None for i in range(1, 9)):
        return None
    if _GD2_TP_OPEN_RE.search(text):
        levels["tp_open"] = True
    return levels


def validate_signal(direction: str, entry_low: float, entry_high: float,
                    stop_loss: float, *tp_args) -> list[str]:
    """
    Returns list of validation error strings. Empty = valid.
    Accepts up to 8 TP values as positional args (tp1 … tp8); extras beyond 8 are ignored.
    """
    errors    = []
    direction = direction.upper()
    if entry_low > entry_high:
        errors.append("entry_low must be <= entry_high")

    # Normalise TP args — positional, up to 8
    tp_list = list(tp_args[:8])  # tp1..tp8

    if direction == "BUY":
        if stop_loss >= entry_low:
            errors.append("Stop Loss must be below entry range for BUY")
        for i, tp in enumerate(tp_list, 1):
            if tp is not None and tp <= entry_high:
                errors.append(f"TP{i} must be above entry range for BUY")
    elif direction == "SELL":
        if stop_loss <= entry_high:
            errors.append("Stop Loss must be above entry range for SELL")
        for i, tp in enumerate(tp_list, 1):
            if tp is not None and tp >= entry_low:
                errors.append(f"TP{i} must be below entry range for SELL")
    else:
        errors.append("Direction must be BUY or SELL")

    active_tps = [t for t in tp_list if t is not None]
    for i in range(len(active_tps) - 1):
        if direction == "BUY" and active_tps[i] >= active_tps[i + 1]:
            errors.append(f"TP{i+1} must be less than TP{i+2} for BUY")
        elif direction == "SELL" and active_tps[i] <= active_tps[i + 1]:
            errors.append(f"TP{i+1} must be greater than TP{i+2} for SELL")

    return errors


# ── AI-derived learned rules ───────────────────────────────────────────────────
# Rules approved via Telegram > Reader Logic > AI (see ai_rule_generator.py):
# a JSON dict of regex patterns auto-generated from one confirmed AI
# extraction and self-validated before ever being saved. apply_learned_rule()
# is the single place both the generator's own validation step and live
# message parsing use to actually run a rule, so there is exactly one
# implementation of "how a rule turns into extracted values" to keep correct.

_TP_NUMBER_RE = re.compile(r"\d+\.?\d*")
_RULE_PATTERN_MAX_LEN = 300


def _safe_compile_rule_pattern(pattern: str):
    if not pattern or len(pattern) > _RULE_PATTERN_MAX_LEN:
        return None
    try:
        return re.compile(pattern, re.IGNORECASE | re.DOTALL)
    except re.error:
        return None


def apply_learned_rule(rule: dict, text: str) -> Optional[dict]:
    """Run one AI-derived rule (gate/direction/entry/sl/tp_block patterns)
    against text. Returns a parse_gold_signal()-shaped dict, or None if the
    rule doesn't match this text at all (a normal, expected outcome — most
    rules only apply to messages of the one shape they were derived from)."""
    gate = _safe_compile_rule_pattern(rule.get("gate_pattern", ""))
    direction_p = _safe_compile_rule_pattern(rule.get("direction_pattern", ""))
    entry_p = _safe_compile_rule_pattern(rule.get("entry_pattern", ""))
    sl_p = _safe_compile_rule_pattern(rule.get("sl_pattern", ""))
    tp_p = _safe_compile_rule_pattern(rule.get("tp_block_pattern", ""))
    if not all([gate, direction_p, entry_p, sl_p, tp_p]):
        return None
    if not gate.search(text):
        return None

    dm = direction_p.search(text)
    if not dm or dm.lastindex != 1:
        return None
    direction = dm.group(1).upper()
    if direction not in ("BUY", "SELL"):
        return None

    em = entry_p.search(text)
    if not em or em.lastindex != 2:
        return None
    try:
        e1, e2 = float(em.group(1)), float(em.group(2))
    except ValueError:
        return None

    slm = sl_p.search(text)
    if not slm or slm.lastindex != 1:
        return None
    try:
        sl_val = float(slm.group(1))
    except ValueError:
        return None

    tpm = tp_p.search(text)
    if not tpm or tpm.lastindex != 1:
        return None
    tps = [float(m) for m in _TP_NUMBER_RE.findall(tpm.group(1))][:8]
    if not tps:
        return None

    raw = {
        "direction": direction,
        "entry_low": min(e1, e2),
        "entry_high": max(e1, e2),
        "stop_loss": sl_val,
    }
    for i in range(1, 9):
        raw[f"tp{i}"] = tps[i - 1] if i <= len(tps) else None
    return _autocorrect_tps(direction, raw["entry_low"], raw["entry_high"], raw)


def parse_with_learned_rules(text: str, channel_name: str) -> Optional[dict]:
    """Try every AI-derived rule saved for this channel (most recent first)
    before the caller ever needs to fall back to a live AI call — the whole
    point of the approve-and-learn workflow. Returns None if no saved rule
    matches, in which case the caller proceeds exactly as before."""
    from forex_trader.core import database as _db
    for rule_row in _db.get_learned_parser_rules(channel_name):
        try:
            rule = json.loads(rule_row.get("pattern") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        parsed = apply_learned_rule(rule, text)
        if parsed and parsed.get("tp1") is not None:
            return parsed
    return None


def apply_sl_adjustment_rule(rule: dict, text: str) -> Optional[float]:
    """Run one AI-derived SL-adjustment rule (gate_pattern, sl_value_pattern)
    against text — the sibling of apply_learned_rule() for follow-up "Adjust
    SL to X" style messages rather than new entry signals. Returns the new
    stop-loss value, or None if the rule doesn't match (expected for most
    messages — each rule only applies to the one channel/wording it was
    derived from)."""
    gate = _safe_compile_rule_pattern(rule.get("gate_pattern", ""))
    sl_p = _safe_compile_rule_pattern(rule.get("sl_value_pattern", ""))
    if not gate or not sl_p:
        return None
    if not gate.search(text):
        return None
    m = sl_p.search(text)
    if not m or m.lastindex != 1:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


def check_sl_adjustment_rules(text: str, channel_name: str) -> Optional[float]:
    """Try every approved ai_derived_sl_adjust rule for this channel before
    the caller falls back to a live AI classification call — same
    approve-and-learn shortcut as parse_with_learned_rules(), for the
    SL-adjustment message category instead of new entries."""
    from forex_trader.core import database as _db
    for rule_row in _db.get_learned_rules_by_type(channel_name, "ai_derived_sl_adjust"):
        try:
            rule = json.loads(rule_row.get("pattern") or "{}")
        except (json.JSONDecodeError, TypeError):
            continue
        result = apply_sl_adjustment_rule(rule, text)
        if result is not None:
            return result
    return None
