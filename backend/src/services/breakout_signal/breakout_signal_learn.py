"""Claude-based batch parameter tuning for the Breakout Engine -- extracted
verbatim (no logic changes) from engine.py's _run_batch_analysis as part of
task 030. See docs/todo/refactor/breakout-signal-migration/030-*.md.

_LearnMixin is composed into BreakoutEngine (breakout_signal_service.py).
"""
from __future__ import annotations

import json as _json
import logging
import time

import backend.src.config as cfg_module
from backend.src.services.ai import provider as ai_provider

from backend.src.services.breakout_signal import breakout_signal_repo as bdb
from backend.src.services.breakout_signal import adaptive_params as ap

_log = logging.getLogger("breakout_signal")

_BATCH_REVIEW_EVERY = 10


class _LearnMixin:
    async def _run_batch_analysis(self) -> None:
        """
        Every _BATCH_REVIEW_EVERY closed trades, ask Claude to review breakout
        signal patterns and recommend adaptive parameter adjustments.
        Mirrors the same mechanism in the bounce engine.
        """
        cfg = cfg_module.load()
        if not ai_provider.is_configured(cfg):
            return

        recent = bdb.get_recent_closed_signals(limit=_BATCH_REVIEW_EVERY)
        if len(recent) < 3:
            return

        stats     = bdb.get_stats()
        by_sess   = bdb.get_perf_by_session()
        by_type   = bdb.get_perf_by_breakout_type()
        by_adx    = bdb.get_perf_by_adx_band()
        by_bias   = bdb.get_perf_by_bias()
        from backend.src.services.breakout_signal import ml_engine as bo_ml
        ml_info   = bo_ml.summary()
        balance   = bdb.get_virtual_balance()

        trade_lines = []
        for s in recent:
            ref = s.get("signal_ref") or f"BO-{s['id']:04d}"
            trade_lines.append(
                f"  {ref} {s.get('direction')} {s.get('breakout_type','?'):8s} "
                f"{(s.get('outcome') or '?').upper():6s}  "
                f"pnl={s.get('pnl_pts', 0):+.1f}pts "
                f"net={s.get('net_pnl_pts', 0) or 0:+.1f}pts "
                f"(${s.get('net_pnl_dollars', 0) or 0:+.2f})  "
                f"session={s.get('session')}  bias={s.get('htf_bias')}  "
                f"adx={s.get('adx_at_signal', 0):.0f}  "
                f"quality={s.get('quality_score', 0):.0%}  "
                f"rr={s.get('rr_tp1', 0):.1f}  ml_prob={s.get('ml_prob') or 'n/a'}"
            )

        sess_lines  = [
            f"  {r.get('session')}: {r.get('wins',0)}W/{r.get('losses',0)}L "
            f"total=${r.get('total_pnl',0) or 0:+.2f}"
            for r in by_sess
        ]
        type_lines  = [
            f"  {r.get('breakout_type')}: {r.get('wins',0)}W/{r.get('losses',0)}L "
            f"total=${r.get('total_pnl',0) or 0:+.2f}"
            for r in by_type
        ]
        adx_lines   = [
            f"  {r.get('adx_band')}: {r.get('wins',0)}W/{r.get('losses',0)}L "
            f"avg=${r.get('avg_pnl',0) or 0:+.2f}"
            for r in by_adx
        ]

        ml_str = (
            f"ML: trained={ml_info['trained']} "
            f"labeled={ml_info['labeled_count']}/{ml_info['min_needed']} "
            f"has_batch={ml_info['has_batch']} has_online={ml_info['has_online']}"
        )

        system_prompt = (
            "You are an algorithmic trading parameter optimizer for a XAUUSD BREAKOUT signal generator. "
            "Analyse the provided trade history and respond ONLY with a single minified JSON object — "
            "no other text:\n"
            '{"adjustments":[{"param":"<name>","new_value":<number>,"reason":"<one sentence>"}],'
            '"summary":"<2-3 sentence analysis>"}\n\n'
            "Available parameters you may adjust:\n"
            + ap.catalogue_for_prompt() +
            "\n\nRules:\n"
            "- Only recommend changes supported by clear evidence in the data.\n"
            "- If sample < 5 or win rate ≥ 55%, return empty adjustments.\n"
            "- Never set a value outside the stated range — it will be clamped anyway.\n"
            "- require_dual_bias must be exactly 0 or 1.\n"
            "- Prefer small incremental changes (±0.05 to ±0.15) over large jumps.\n"
            "- For breakouts: higher ADX thresholds reduce false signals but also reduce trades."
        )

        user_prompt = "\n".join([
            f"Virtual account balance: ${balance:.2f} (started $1,000.00)",
            f"Overall: {stats['wins']}W / {stats['losses']}L / {stats['be']}BE  "
            f"win_rate={stats['win_rate']}%  avg_pnl=${stats['avg_pnl_dollars']:+.2f}",
            ml_str,
            "",
            f"Last {len(recent)} trades:",
            *trade_lines,
            "",
            "Performance by session:",
            *sess_lines,
            "",
            "Performance by breakout type (go vs retest):",
            *type_lines,
            "",
            "Performance by ADX band:",
            *adx_lines,
        ])

        try:
            raw = await ai_provider.complete(cfg, system_prompt, user_prompt, max_tokens=400, timeout=25)
            if not raw:
                _log.warning("[BO-Engine] Batch analysis: empty response from AI")
                bdb.log_analysis({
                    "ts":              time.time(),
                    "result":          "batch_analysis_failed",
                    "claude_decision": f"Batch analysis failed ({len(recent)} trades): empty response from AI",
                })
                return
            data = _json.loads(raw)

            adjustments = data.get("adjustments", [])
            applied     = []
            for adj in adjustments:
                param  = adj.get("param", "")
                value  = adj.get("new_value")
                reason = adj.get("reason", "")
                if param and value is not None:
                    new_v = ap.apply_adjustment(param, float(value), reason)
                    if new_v is not None:
                        applied.append(f"{param}→{new_v:.4g}")

            applied_str = (", ".join(applied)) if applied else "no changes"
            summary     = data.get("summary", "")
            _log.info(
                "[BO-Engine] Batch analysis (%d trades): %s. %s",
                len(recent), applied_str, summary[:120],
            )
            bdb.log_analysis({
                "ts":              time.time(),
                "result":          f"batch_analysis:{len(recent)}_trades",
                "claude_decision": f"Batch ({len(recent)} trades): {applied_str}. {summary}",
            })

        except Exception as e:
            err_msg = f"Batch analysis failed ({len(recent)} trades): {e}"
            _log.warning("[BO-Engine] %s", err_msg)
            bdb.log_analysis({
                "ts":              time.time(),
                "result":          "batch_analysis_failed",
                "claude_decision": err_msg,
            })
