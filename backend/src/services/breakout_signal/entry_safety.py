"""Generation eligibility shared by the candle and fast entry routes."""
import logging
import time
from backend.src.db import database
from backend.src.services.cluster import node_roles
from backend.src.services.market import entry_contract


async def generation_permitted():
    if not await database.to_db_thread(node_roles.engines_generate_here):
        return False
    allowed, _ = await database.to_db_thread(database.is_session_allowed)
    return bool(allowed)


def quote_problem(tick, max_spread):
    if not tick:
        return "quote unavailable"
    problem = (entry_contract.quote_refusal(tick.bid, tick.ask)
               or entry_contract.broker_quote_refusal(tick, time.time()))
    if problem:
        return problem
    if float(tick.ask) - float(tick.bid) > max_spread:
        return "spread exceeds entry limit"
    return None


def source_problem(tick, context, now):
    if getattr(tick, "timestamp", None) is None or context.get("source_closed_at") is None:
        return None  # legacy adapter/source timing is unknown
    return entry_contract.age_refusal(context["source_closed_at"], now, 300)


async def final_refused(tick, log_entry, velocity, now=None):
    """Recheck after review. Calls without a quote retain unknown timing."""
    if tick is None:
        return False
    from backend.src.services.breakout_signal import adaptive_params as ap, breakout_signal_repo as repo
    now = time.time() if now is None else now
    problem = quote_problem(tick, ap.get("max_spread_pts"))
    problem = problem or source_problem(tick, log_entry, now)
    problem = problem or entry_contract.age_refusal(log_entry.get("ts"), now, 120)
    if not await generation_permitted():
        problem = "generation permission or Trading Markets changed"
    if not problem:
        return False
    log_entry.update(result="final_entry_refused", suppressed_reason=problem)
    if not velocity:
        repo.log_analysis(log_entry)
    logging.getLogger("breakout_signal").info("[BO-Entry] %s", problem)
    return True
