"""Claude-based batch parameter tuning for TestSignalEngine (Bounce) --
extracted verbatim (no logic changes) from engine.py's _run_batch_analysis
as part of task 030. See docs/todo/refactor/test-signal-migration/030-*.md.

_LearnMixin is composed into TestSignalEngine (test_signal_service.py).
"""
from __future__ import annotations

import logging
import time

import backend.src.config as cfg_module
from backend.src.services.ai import provider as ai_provider

from backend.src.services.test_signal import test_signal_repo as tdb
from backend.src.services.test_signal import adaptive_params as ap

_log = logging.getLogger("test_signal")

_BATCH_REVIEW_EVERY = 10


def _fetch_tg_outcomes(max_age_days: int = 7) -> list[dict]:
    """Return TG signal rows with a success flag from the main DB.
    Success = signal's trade reached TP1 (partial close with reason LIKE 'TP1%')."""
    import time as _t
    try:
        from forex_trader.core import database as _main_db
        cutoff = _t.time() - max_age_days * 86400
        with _main_db.db() as conn:
            rows = conn.execute(
                """SELECT vs.source_name, vs.direction,
                          CASE WHEN tp1_hit.trade_id IS NOT NULL THEN 1 ELSE 0 END as success
                   FROM vantage_signals vs
                   LEFT JOIN vantage_simulated_trades vst ON vst.signal_id = vs.signal_id
                   LEFT JOIN (
                       SELECT DISTINCT trade_id FROM vantage_partial_closes
                       WHERE reason LIKE 'TP1%' AND lots_closed > 0
                   ) tp1_hit ON tp1_hit.trade_id = vst.trade_id
                   WHERE vs.created_at >= ?
                   ORDER BY vs.created_at DESC LIMIT 100""",
                (cutoff,),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        _log.debug("[TestSignal] TG outcome fetch error: %s", e)
        return []


class _LearnMixin:
    async def _run_batch_analysis(self) -> None:
        """
        Every _BATCH_REVIEW_EVERY trades, ask Claude to analyse patterns and
        recommend specific parameter adjustments.  Adjustments are applied
        automatically (clamped to safe ranges) and logged.
        """
        cfg = cfg_module.load()
        if not ai_provider.is_configured(cfg):
            return

        recent = tdb.get_recent_closed_signals(limit=_BATCH_REVIEW_EVERY)
        if len(recent) < 3:
            return

        balance   = tdb.get_virtual_balance()
        stats     = tdb.get_stats()
        by_sess   = tdb.get_perf_by_session()
        by_level  = tdb.get_perf_by_level_type()
        by_bias   = tdb.get_perf_by_bias()
        tg_rows   = _fetch_tg_outcomes()

        trade_lines = []
        by_regime: dict[str, dict] = {}
        for s in recent:
            ref = s.get("signal_ref") or f"SIG-{s['id']:04d}"
            fallback_flag = " [fallback-approved]" if s.get("claude_fallback") else ""
            regime = s.get("regime") or "neutral"
            trade_lines.append(
                f"  {ref} {s.get('direction')} {(s.get('outcome') or '?').upper():6s}  "
                f"pnl={s.get('pnl_pts', 0):+.1f}pts (${s.get('pnl_dollars', 0):+.2f})  "
                f"session={s.get('session')}  bias={s.get('htf_bias')}  regime={regime} "
                f"adx={s.get('adx') or 0:.0f}  "
                f"level={s.get('key_level_type')}  quality={s.get('quality_score', 0):.0%}  "
                f"rr={s.get('rr_tp1', 0):.1f}{fallback_flag}"
            )
            bucket = by_regime.setdefault(regime, {"wins": 0, "losses": 0, "pnl": 0.0})
            if s.get("outcome") == "win":
                bucket["wins"] += 1
            elif s.get("outcome") == "loss":
                bucket["losses"] += 1
            bucket["pnl"] += float(s.get("pnl_dollars", 0) or 0)

        sess_lines = [
            f"  {r.get('session')}: {r.get('wins',0)}W/{r.get('losses',0)}L "
            f"total=${r.get('total_pnl',0):+.2f}"
            for r in by_sess
        ]
        level_lines = [
            f"  {r.get('key_level_type')}: {r.get('wins',0)}W/{r.get('losses',0)}L "
            f"total=${r.get('total_pnl',0):+.2f}"
            for r in by_level
        ]
        regime_lines = [
            f"  {regime}: {b['wins']}W/{b['losses']}L total=${b['pnl']:+.2f}"
            for regime, b in sorted(by_regime.items())
        ]

        tg_source_stats: dict = {}
        for row in tg_rows:
            src = row.get("source_name") or "unknown"
            if src not in tg_source_stats:
                tg_source_stats[src] = {"total": 0, "wins": 0}
            tg_source_stats[src]["total"] += 1
            tg_source_stats[src]["wins"]  += int(row.get("success", 0))
        tg_lines = [
            f"  {src}: {d['wins']}/{d['total']} TP1-success"
            for src, d in sorted(tg_source_stats.items())
        ]

        system_prompt = (
            "You are an algorithmic trading parameter optimizer for a XAUUSD signal generator. "
            "Analyse the provided trade history and respond ONLY with a single minified JSON object "
            "matching this exact schema — no other text:\n"
            '{"adjustments":[{"param":"<name>","new_value":<number>,"regime":"<trending|ranging|neutral|null>",'
            '"reason":"<one sentence>"}],'
            '"summary":"<2-3 sentence analysis of what drove wins vs losses>"}\n\n'
            "Available parameters you may adjust (only include a param if genuinely supported by data):\n"
            + ap.catalogue_for_prompt() +
            "\n\nCurrent per-regime learned values (params marked 'learnable per-regime' above):\n"
            + ap.regime_catalogue_for_prompt() +
            "\n\nRules:\n"
            "- Only recommend changes supported by clear evidence in the data.\n"
            "- If sample < 5 trades or performance is acceptable (win rate >= 55%), return empty adjustments.\n"
            "- Never set a value outside the stated range — it will be clamped anyway.\n"
            "- allow_asian and allow_counter_bias must be exactly 0 or 1.\n"
            "- Prefer small incremental changes (±0.05 to ±0.15) over large jumps.\n"
            "- For params marked 'learnable per-regime': if losses are concentrated in ONE regime "
            "(see regime breakdown below), set \"regime\" to that regime so the fix only applies there "
            "and does not affect performance in other regimes. Set \"regime\" to null (or omit it) for "
            "a genuinely global adjustment, or for any param not marked per-regime."
        )

        user_prompt_parts = [
            f"Virtual account balance: ${balance:.2f} (started $1,000.00)",
            f"Overall stats: {stats['wins']}W / {stats['losses']}L / {stats['be']}BE  "
            f"win_rate={stats['win_rate']}%  avg_pnl=${stats['avg_pnl_dollars']:+.2f}",
            "",
            f"Last {len(recent)} trades:",
            *trade_lines,
            "",
            "Performance by market regime (this batch):",
            *regime_lines,
            "",
            "Performance by session:",
            *sess_lines,
            "",
            "Performance by level type:",
            *level_lines,
        ]
        if tg_lines:
            user_prompt_parts += ["", "Telegram provider TP1-success rates (last 7 days):"] + tg_lines
        user_prompt = "\n".join(user_prompt_parts)

        try:
            import json as _json

            raw = await ai_provider.complete(cfg, system_prompt, user_prompt, max_tokens=500, timeout=25)
            if raw.startswith("```"):
                lines = raw.splitlines()
                raw = "\n".join(lines[1:])
                if raw.endswith("```"):
                    raw = raw[:-3].strip()
            data = _json.loads(raw)

            summary     = data.get("summary", "")
            adjustments = data.get("adjustments", [])
            applied     = []

            for adj in adjustments:
                param  = adj.get("param", "")
                value  = adj.get("new_value")
                reason = adj.get("reason", "")
                regime_tag = adj.get("regime") or None
                if param and value is not None:
                    new_v = ap.apply_adjustment(param, float(value), reason, regime=regime_tag)
                    if new_v is not None:
                        tag = f"[{regime_tag}]" if regime_tag else ""
                        applied.append(f"{param}{tag}→{new_v:.4g}")

            applied_str = (", ".join(applied)) if applied else "no changes"
            log_msg = f"Batch analysis ({len(recent)} trades): {applied_str}. {summary}"

            tdb.log_analysis({
                "ts":              time.time(),
                "result":          f"batch_analysis:{len(recent)}_trades",
                "claude_decision": log_msg,
            })
            _log.info("[TestSignal] %s", log_msg[:200])

        except Exception as e:
            err_msg = f"Batch analysis failed ({len(recent)} trades): {e}"
            tdb.log_analysis({
                "ts":              time.time(),
                "result":          "batch_analysis_failed",
                "claude_decision": err_msg[:300],
            })
            _log.warning("[TestSignal] %s", err_msg)
