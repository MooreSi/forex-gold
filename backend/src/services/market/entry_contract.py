"""Pure decision observations and final executable-price checks."""
from __future__ import annotations
import hashlib
import json
import logging
import math
import time

log = logging.getLogger(__name__)
LABEL_VERSION = "executable-quotes-v2"


def _json(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def quote_refusal(bid, ask):
    try:
        bid, ask = float(bid), float(ask)
        if not all(math.isfinite(x) for x in (bid, ask)) or bid <= 0 or ask < bid:
            return "invalid executable quote"
    except (TypeError, ValueError):
        return "invalid executable quote"
    return None


def age_refusal(created_at, now, max_age_s):
    try:
        age = float(now) - float(created_at)
        if not math.isfinite(age) or age < -1 or age > max_age_s:
            return "stale or future entry observation"
    except (TypeError, ValueError):
        return "entry observation time unavailable"
    return None


def broker_quote_refusal(tick, now):
    """MT5 timestamps are broker time. Legacy adapters without it stay unknown."""
    from backend.src.services.market import bar_clock
    stamp = getattr(tick, "timestamp", None)
    if stamp is None:
        return None
    try:
        if not math.isfinite(float(stamp)):
            return "invalid quote time"
        offset = bar_clock.broker_offset_s(float(stamp), now)
        if offset is None:
            return "broker quote time unavailable"
        return age_refusal(float(stamp) - offset, now, 60)
    except (TypeError, ValueError):
        return "invalid quote time"


def snapshot(engine, stage, policy, context, features, observed_at):
    if not math.isfinite(float(observed_at)):
        raise ValueError("observation time must be finite")
    # Serialisation copies nested values and refuses NaN, rather than recording
    # a plausible replacement for something the process never observed.
    if isinstance(features, str):
        features = json.loads(features)
    body = json.loads(_json({"engine": engine, "stage": stage, "policy": policy,
        "context": context, "features": features, "observed_at": observed_at,
        "label_version": LABEL_VERSION, "source_availability": "not measured"}))
    body["policy_hash"] = digest(policy)
    body["schema_hash"] = digest(sorted(features) if isinstance(features, dict)
                                 else context.get("feature_names") or {"unidentified_width": len(features)})
    return body


def selected_policy(source, strategy=None):
    from backend.src import config
    from backend.src.services.risk.schedule import effective_channel_strategy
    from backend.src.services.broker import ea_templates
    strategy = strategy or effective_channel_strategy(source)
    if not strategy:
        from backend.src.db import database
        strategy = (database.get_risk_settings() or {}).get("trade_strategy")
    template = (ea_templates.get_ea_template(ea_templates.template_name_from_override(strategy))
                if ea_templates.is_template_override(strategy or "") else None)
    return {"strategy": strategy, "template": template, "environment": config.get("account_env", "unknown"),
            "label_version": LABEL_VERSION,
            "entry_timing": {"quote_max_age_s": 60, "breakout_context_max_age_s": 120,
                             "trend_source_max_age_s": 900, "legacy_adapter_time": "unknown"},
            "status": "unresolved_auto" if strategy == "auto" else
                      "observed" if strategy and (template is not None or not str(strategy).startswith("template:"))
                      else "unavailable"}


def observe(repo, engine, signal_ref, stage, context, features, *, source, strategy=None, policy=None):
    """Best-effort measurement only; never changes an execution decision."""
    try:
        from backend.src.services.market import decision_repo
        observation = snapshot(engine, stage, policy if policy is not None else selected_policy(source, strategy),
                               context, features, time.time())
        decision_repo.append(repo.get_db(), str(signal_ref), observation)
        return observation
    except Exception as exc:
        log.warning("[%s] decision observation unavailable: %s", engine, exc)
        return None
