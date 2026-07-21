"""
Nightly Telegram research job for GD Copy.

Reads the day's Gold Diggers VIP + GD2 messages (including chart images),
asks the AI provider selected in Settings > AI to synthesise the real
trader's risk-management/entry-logic behaviour that day into two numeric
scores, feeds those scores into ml_engine's feature set as live inputs,
forces an immediate retrain, and emails a summary of what was learned.

Called once nightly (22:00 Europe/London) by
SimulationEngine._gd_copy_research_loop in core/engine.py.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Optional
from zoneinfo import ZoneInfo

from forex_trader.core import ai_provider

_log = logging.getLogger(__name__)

_VIP_GROUP_ID = "1608388054"
_GD2_GROUP_ID = "2616846888"
_GROUP_IDS = [_VIP_GROUP_ID, _GD2_GROUP_ID]

_MAX_IMAGES = 12          # per-night cap — cost/time control
_MAX_TEXT_MESSAGES = 400  # per-night cap on messages included in the text prompt

_SYSTEM_PROMPT = (
    "You are a professional trading mentor analysing one day's messages from "
    "two live gold-trading Telegram signal channels (Gold Diggers VIP and "
    "GOLD DIGGERS 2.0), including any chart screenshots posted. Your job is "
    "to extract concrete, honest observations about the real trader's risk "
    "management discipline and entry/exit logic that day — not to praise or "
    "criticise, just observe. Respond with ONLY a JSON object, no other text, "
    "matching exactly this schema:\n"
    '{"summary": "3-6 sentence plain-English synthesis of the day",'
    ' "data_sufficient": <true only if there were enough live trade-management '
    'messages today to actually judge discipline/aggression from real behaviour; '
    'false on a quiet day with no signals, only recaps/chatter/marketing, or too '
    'little to responsibly score — when false, still fill every other field with '
    'your best qualitative read, but discipline_score/aggression_score are ignored '
    'by the caller, so their exact value does not matter>,'
    ' "discipline_score": <0.0-1.0, 1.0 = strictly respected stated SL/TP and '
    'position sizing all day, 0.0 = repeatedly moved SL against the trade, '
    'oversized, or abandoned stated risk plans>,'
    ' "aggression_score": <0.0-1.0, 1.0 = aggressively scaled into moves, '
    'chased entries, added to losers, 0.0 = patient, waited for clean setups, '
    'single entries>,'
    ' "notable_trades": "1-3 sentences on the most notable win or loss and why",'
    ' "entry_logic_notes": "1-2 sentences on what entry triggers were actually used",'
    ' "risk_management_notes": "1-2 sentences on stop/position-sizing behavior"}'
)


def _uk_today_str() -> str:
    return datetime.now(ZoneInfo("Europe/London")).strftime("%Y-%m-%d")


def _uk_midnight_utc_iso() -> str:
    uk_now = datetime.now(ZoneInfo("Europe/London"))
    uk_midnight = uk_now.replace(hour=0, minute=0, second=0, microsecond=0)
    return uk_midnight.astimezone(ZoneInfo("UTC")).isoformat()


async def _download_images(engine, messages: list[dict]) -> list[bytes]:
    """Download up to _MAX_IMAGES photo attachments via the live TelegramReader
    client — reuses the already-authenticated session rather than opening a
    second one. Skips silently (logs) on any per-image failure so one bad
    download doesn't sink the whole night's research."""
    reader = getattr(engine, "_tg_reader", None)
    client = getattr(reader, "_client", None) if reader else None
    if client is None:
        return []

    photo_msgs = [m for m in messages if m.get("media_type") == "photo"]
    photo_msgs = list(reversed(photo_msgs))[:_MAX_IMAGES]  # most recent first

    by_group: dict[str, list[int]] = {}
    for m in photo_msgs:
        by_group.setdefault(str(m["group_id"]), []).append(int(m["telegram_message_id"]))

    images: list[bytes] = []
    for gid, msg_ids in by_group.items():
        try:
            entity = await client.get_entity(int(gid))
            tg_msgs = await client.get_messages(entity, ids=msg_ids)
            for tg_msg in tg_msgs:
                if tg_msg is None:
                    continue
                try:
                    data = await client.download_media(tg_msg, file=bytes)
                    if data:
                        images.append(data)
                except Exception as e:
                    _log.debug("[GDC-Research] image download failed for msg %s: %s", tg_msg.id, e)
        except Exception as e:
            _log.warning("[GDC-Research] entity/message fetch failed for group %s: %s", gid, e)

    return images


def _build_prompt(messages: list[dict]) -> str:
    recent = messages[-_MAX_TEXT_MESSAGES:]
    lines = []
    for m in recent:
        ch = m.get("group_name") or m.get("group_id")
        ts = m.get("timestamp") or m.get("received_at") or ""
        text = (m.get("text") or "").strip()
        if not text:
            if m.get("has_media"):
                lines.append(f"[{ch} {ts}] (image, no caption)")
            continue
        lines.append(f"[{ch} {ts}] {text}")
    body = "\n".join(lines) if lines else "(no text messages today)"
    truncated_note = (
        f"\n\n(showing the most recent {len(recent)} of {len(messages)} messages today)"
        if len(messages) > len(recent) else ""
    )
    return (
        f"Today's messages from Gold Diggers VIP and GOLD DIGGERS 2.0 "
        f"({len(messages)} total):\n\n{body}{truncated_note}\n\n"
        "Analyse the day and respond with the JSON object described in the system prompt."
    )


def _parse_ai_json(raw: str) -> Optional[dict]:
    raw = raw.strip()
    if raw.startswith("```"):
        raw = raw.strip("`")
        if raw.startswith("json"):
            raw = raw[4:]
    try:
        data = json.loads(raw)
    except Exception:
        return None
    return data if isinstance(data, dict) else None


def _clamp01(v, default: float = 0.5) -> float:
    try:
        return max(0.0, min(1.0, float(v)))
    except Exception:
        return default


async def _send_failure_alert(date_str: str, n_messages: int, reason: str) -> None:
    """A failed nightly research run used to fail completely silently — the
    only way to notice was the absence of the usual summary email, which is
    indistinguishable from a quiet day with no signals (see no_messages,
    which deliberately does NOT alert since that's a legitimate, common
    outcome). Confirmed live: an invalid Anthropic API key broke this run
    for a full night (2026-07-14) with nothing surfaced anywhere the user
    would see it. This sends a short, distinct alert only for genuine
    failures (AI call error, unparseable response) so silence always means
    'nothing to report' and never 'something broke'."""
    from forex_trader.core import email_service
    html = (
        f'<div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;">'
        f'<h2 style="color:#ef4444;">GD Copy — Nightly Research FAILED</h2>'
        f'<p style="color:#6b7280;font-size:13px;">{date_str} &middot; {n_messages} messages were '
        f'available but the run did not complete.</p>'
        f'<p style="color:#374151;">Reason: <code>{reason}</code></p>'
        f'<p style="color:#6b7280;font-size:12px;">Check Settings &gt; AI for a valid API key/provider '
        f'configuration, or the app log for the full error. No summary was generated and the ML model '
        f'was not retrained tonight.</p>'
        f'</div>'
    )
    try:
        ok, err = await email_service.send_email(
            f"GD Copy — Nightly Research FAILED — {date_str}", html,
        )
        if not ok:
            _log.warning("[GDC-Research] failure-alert email also failed to send: %s", err)
    except Exception as e:
        _log.warning("[GDC-Research] failure-alert email build/send error: %s", e)


async def run_nightly_research(engine) -> dict:
    """Entry point called once nightly by SimulationEngine._gd_copy_research_loop.
    Safe to call more than once for the same date — gdc_daily_research is
    keyed by date and store_daily_research does INSERT OR REPLACE, and a
    duplicate run simply overwrites the cached scores and retrains again
    rather than corrupting anything."""
    from forex_trader.gd_copy_signal import gd_copy_signal_repo as gdc_db
    from forex_trader.gd_copy_signal import ml_engine as gdc_ml
    from forex_trader.core import database as db_module
    from forex_trader.core import email_service

    cfg = engine._cfg
    # engine._cfg is the same live-updated dict Settings > AI's save handlers
    # mutate directly (see ui/pages/settings.py's _save_provider()) and that
    # ai_provider.complete()/complete_vision() already dispatch on via
    # cfg["ai_provider"] — this job was never hardcoded to Claude. Logged
    # explicitly here so which provider actually ran is never ambiguous from
    # the log alone (a "AI call failed" 401 elsewhere in the log — e.g. the
    # daily model-catalogue refresh checking a stale/invalid Claude key —
    # does not mean *this* job used Claude; confirmed live 2026-07-16 that a
    # run failure was misattributed to exactly that unrelated Claude error
    # before this line existed to rule it out at a glance).
    _log.info("[GDC-Research] using AI provider: %s", cfg.get("ai_provider", "claude"))
    if not ai_provider.is_configured(cfg):
        _log.info("[GDC-Research] skipped — no AI provider configured")
        return {"ran": False, "reason": "ai_not_configured"}

    date_str = _uk_today_str()
    since_iso = _uk_midnight_utc_iso()

    messages = db_module.get_messages_for_research(_GROUP_IDS, since_iso)
    if not messages:
        _log.info("[GDC-Research] no messages today (%s) — skipping", date_str)
        return {"ran": False, "reason": "no_messages", "date": date_str}

    images = await _download_images(engine, messages)
    prompt = _build_prompt(messages)

    raw = None
    images_analyzed = 0
    # Generous timeouts — this is a once-nightly background job with no
    # latency requirement, unlike every other AI call site in the app that
    # blocks a live trading decision. Confirmed live 2026-07-15: a 283-
    # message night (the largest _MAX_TEXT_MESSAGES-capped prompt this job
    # sends) timed out DeepSeek at the previous 60s limit — ReadTimeout with
    # no further detail, since httpx's own timeout exceptions carry no
    # message text (see the repr(e) logging below, not str(e), for exactly
    # this reason). Widened well past any plausible real synthesis time
    # rather than tuned to the one observed failure, since a slower provider
    # response or a heavier message night should never need another manual bump.
    _RESEARCH_TIMEOUT_S = 240
    if images:
        try:
            raw = await ai_provider.complete_vision(
                cfg, _SYSTEM_PROMPT, prompt, images, max_tokens=1500, timeout=_RESEARCH_TIMEOUT_S
            )
            images_analyzed = len(images)
        except Exception as e:
            # Vision failed on whichever provider is selected in Settings >
            # AI (missing key, or that provider/model can't handle images) —
            # fall back to a text-only call on the SAME provider rather than
            # silently switching to a different one the user didn't choose.
            _log.info(
                "[GDC-Research] vision call failed on %s (%s) — falling back to text-only synthesis",
                cfg.get("ai_provider", "claude"), repr(e),
            )
    if raw is None:
        try:
            raw = await ai_provider.complete(
                cfg, _SYSTEM_PROMPT, prompt, max_tokens=1500, timeout=_RESEARCH_TIMEOUT_S
            )
        except Exception as e:
            _log.warning("[GDC-Research] AI call failed: %s", repr(e))
            await _send_failure_alert(date_str, len(messages), f"AI call failed: {e!r}")
            return {"ran": False, "reason": f"ai_error: {e!r}", "date": date_str}

    parsed = _parse_ai_json(raw)
    if parsed is None:
        _log.warning("[GDC-Research] could not parse AI response as JSON: %s", raw[:300])
        await _send_failure_alert(date_str, len(messages), "AI response could not be parsed as JSON")
        return {"ran": False, "reason": "parse_error", "date": date_str}

    data_sufficient = bool(parsed.get("data_sufficient", True))
    summary     = str(parsed.get("summary") or "")[:2000]
    notable     = str(parsed.get("notable_trades") or "")[:1000]
    entry_notes = str(parsed.get("entry_logic_notes") or "")[:1000]
    risk_notes  = str(parsed.get("risk_management_notes") or "")[:1000]

    if data_sufficient:
        discipline = _clamp01(parsed.get("discipline_score"))
        aggression = _clamp01(parsed.get("aggression_score"))
    else:
        # Too little real trade-management activity today to responsibly
        # score — carry forward yesterday's cached value rather than writing
        # a low-confidence number (e.g. a quiet day's 0.0) into the live ML
        # feature, which would misrepresent "no data" as "worst behaviour".
        discipline, aggression = gdc_ml.get_daily_research_scores()
        _log.info("[GDC-Research] %s — insufficient data, carrying forward prior scores (%.2f/%.2f)",
                   date_str, discipline, aggression)

    gdc_db.store_daily_research(
        date_str, discipline, aggression, summary, notable, entry_notes, risk_notes,
        len(messages), images_analyzed, json.dumps(parsed),
    )
    gdc_db.set_config("vip_discipline_score", str(discipline))
    gdc_db.set_config("vip_aggression_score", str(aggression))

    try:
        gdc_ml.retrain_now()
        retrained = True
    except Exception as e:
        _log.warning("[GDC-Research] retrain failed: %s", e)
        retrained = False

    try:
        html = _build_email_html(
            date_str, discipline, aggression, summary, notable,
            entry_notes, risk_notes, len(messages), images_analyzed, retrained, data_sufficient,
        )
        ok, err = await email_service.send_email(
            f"GD Copy — Nightly Telegram Research — {date_str}", html,
        )
        if not ok:
            _log.warning("[GDC-Research] email send failed: %s", err)
    except Exception as e:
        _log.warning("[GDC-Research] email build/send error: %s", e)

    _log.info(
        "[GDC-Research] %s — %d messages, %d images, discipline=%.2f aggression=%.2f retrained=%s",
        date_str, len(messages), images_analyzed, discipline, aggression, retrained,
    )
    return {
        "ran": True, "date": date_str, "n_messages": len(messages), "n_images": images_analyzed,
        "discipline_score": discipline, "aggression_score": aggression, "retrained": retrained,
    }


def _build_email_html(date_str, discipline, aggression, summary, notable,
                       entry_notes, risk_notes, n_messages, n_images, retrained,
                       data_sufficient) -> str:
    def _bar(label, value):
        pct = round(value * 100)
        return (
            f'<tr><td style="padding:4px 12px 4px 0;color:#9ca3af;font-size:13px;">{label}</td>'
            f'<td style="width:200px;"><div style="background:#1f2937;border-radius:4px;overflow:hidden;">'
            f'<div style="width:{pct}%;background:#f59e0b;height:10px;"></div></div></td>'
            f'<td style="padding-left:8px;color:#e5e7eb;font-size:13px;">{value:.2f}</td></tr>'
        )
    retrain_note = "retrained just now" if retrained else "NOT retrained — check the app log"
    carried_forward_note = (
        '<p style="color:#f59e0b;font-size:12px;">Not enough live trade-management '
        'activity today to score discipline/aggression — scores below are carried '
        'forward unchanged from the last day that had enough data.</p>'
        if not data_sufficient else ""
    )
    return f"""
    <div style="font-family:Arial,sans-serif;max-width:640px;margin:0 auto;">
      <h2 style="color:#f59e0b;">GD Copy — Nightly Telegram Research</h2>
      <p style="color:#6b7280;font-size:13px;">{date_str} &middot; {n_messages} messages analysed &middot; {n_images} chart images reviewed</p>
      {carried_forward_note}
      <table style="border-collapse:collapse;margin:16px 0;">
        {_bar("Discipline", discipline)}
        {_bar("Aggression", aggression)}
      </table>
      <h3 style="color:#111827;">Summary</h3>
      <p style="color:#374151;">{summary}</p>
      <h3 style="color:#111827;">Notable trades</h3>
      <p style="color:#374151;">{notable or "None noted"}</p>
      <h3 style="color:#111827;">Entry logic observed</h3>
      <p style="color:#374151;">{entry_notes or "None noted"}</p>
      <h3 style="color:#111827;">Risk management observed</h3>
      <p style="color:#374151;">{risk_notes or "None noted"}</p>
      <p style="color:#6b7280;font-size:12px;margin-top:24px;">
        These two scores are now feeding GD Copy's ML model as live features
        (vip_discipline_score / vip_aggression_score), and the model was {retrain_note}
        to incorporate them.
      </p>
    </div>
    """
