"""Repairing a malformed take-profit ladder on a freshly parsed signal.

Lifted out of parser.py, which sat 96 lines over the 800-line ceiling. Called
from three parser entry points and from the AI signal extractor, and re-exported
from parser so those callers are unchanged.
"""
from __future__ import annotations

import logging

_log = logging.getLogger(__name__)


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
